from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from . import cache, db
from .strength import (
    DEFAULT_FORMULA,
    FORMULAS,
    LiftError,
    compare,
    deload,
    estimates,
    formulas_agree,
    is_personal_best,
    one_rep_max,
    plateau,
    volume,
    warmup_sets,
)

app = FastAPI(title="gym-tracker")

PB_TTL = 7 * 86400


@app.get("/health")
def health():
    out = {"status": "ok", "postgres": False, "redis": False}
    try:
        db.query("SELECT 1")
        out["postgres"] = True
    except Exception as e:
        out["pg_error"] = str(e)
    try:
        cache.client().ping()
        out["redis"] = True
    except Exception as e:
        out["redis_error"] = str(e)
    return out if out["postgres"] and out["redis"] else JSONResponse(out, status_code=503)


def _exercise(name_or_id):
    if str(name_or_id).isdigit():
        row = db.one("SELECT id, name, category FROM exercises WHERE id=%s",
                     (int(name_or_id),))
    else:
        row = db.one("SELECT id, name, category FROM exercises WHERE lower(name)=lower(%s)",
                     (str(name_or_id),))
    if not row:
        raise HTTPException(404, f"no exercise called {name_or_id!r}")
    return row


def _pb(user, ex_id, formula=DEFAULT_FORMULA):
    """Current personal best, from Redis, warmed from Postgres on a miss.

    This is read on every single set logged, which is exactly why it is not a
    SELECT max(...) over the whole lift history each time.
    """
    c = cache.client()
    key = f"pb:{user}:{ex_id}:{formula}"
    hit = c.hgetall(key)
    if hit:
        return {"weight": float(hit["weight"]), "reps": int(hit["reps"]),
                "e1rm": float(hit["e1rm"]), "on": hit.get("on", ""), "source": "redis"}
    rows = db.query("SELECT weight, reps, performed_at FROM lifts"
                    " WHERE user_name=%s AND exercise_id=%s", (user, ex_id))
    best = None
    for r in rows:
        try:
            e = one_rep_max(float(r["weight"]), int(r["reps"]), formula)
        except LiftError:
            continue
        if best is None or e > best["e1rm"]:
            best = {"weight": float(r["weight"]), "reps": int(r["reps"]),
                    "e1rm": e, "on": str(r["performed_at"])[:10]}
    if best:
        c.hset(key, mapping={k: str(v) for k, v in best.items()})
        c.expire(key, PB_TTL)
        return {**best, "source": "postgres"}
    return None


def _store_pb(user, ex_id, formula, pb):
    c = cache.client()
    key = f"pb:{user}:{ex_id}:{formula}"
    c.hset(key, mapping={k: str(v) for k, v in pb.items()})
    c.expire(key, PB_TTL)


@app.get("/exercises")
def exercises():
    rows = db.query("SELECT e.id, e.name, e.category,"
                    " (SELECT count(*) FROM lifts l WHERE l.exercise_id=e.id) AS sets_logged"
                    " FROM exercises e ORDER BY e.category, e.name")
    return {"exercises": rows}


@app.post("/sets", status_code=201)
def log_set(payload: dict = Body(...)):
    user = str(payload.get("user", "")).strip()
    if not user:
        raise HTTPException(400, "user is required")
    ex = _exercise(payload.get("exercise", ""))
    formula = str(payload.get("formula", DEFAULT_FORMULA)).lower()
    if formula not in FORMULAS:
        raise HTTPException(400, f"unknown formula {formula!r} - have {sorted(FORMULAS)}")
    try:
        weight = float(payload.get("weight"))
        reps = int(payload.get("reps"))
        both = estimates(weight, reps)
    except (TypeError, ValueError) as e:
        raise HTTPException(400, str(e) if isinstance(e, LiftError)
                            else "weight and reps are required numbers")

    # Every formula gets its own cache key, so every formula's key has to be
    # judged against this set. Updating only the one the caller asked for
    # leaves the other holding a number that is quietly wrong until it
    # happens to expire - and a stale personal best is worse than no cache.
    # All reads happen before the INSERT, or a cold warm would include the
    # set we are about to compare against itself.
    verdicts = {f: is_personal_best(weight, reps, _pb(user, ex["id"], f), f)
                for f in FORMULAS}
    row = db.one("INSERT INTO lifts (user_name, exercise_id, weight, reps, notes)"
                 " VALUES (%s,%s,%s,%s,%s)"
                 " RETURNING id, user_name, weight, reps, notes, performed_at",
                 (user, ex["id"], weight, reps, str(payload.get("notes", ""))[:200]))
    for f, v in verdicts.items():
        if v["personal_best"]:
            _store_pb(user, ex["id"], f, {"weight": weight, "reps": reps,
                                          "e1rm": v["e1rm"],
                                          "on": str(row["performed_at"])[:10]})
    verdict = verdicts[formula]
    return {"id": row["id"], "user": user, "exercise": ex["name"],
            "weight": float(row["weight"]), "reps": row["reps"],
            "notes": row.get("notes", ""),
            "performed_at": row["performed_at"],
            "estimates": both, "verdict": verdict,
            "verdict_by_formula": verdicts}


@app.get("/sets")
def list_sets(user: str = None, limit: int = 50):
    if user:
        rows = db.query(
            "SELECT l.id, l.user_name, l.exercise_id, e.name AS exercise, "
            "l.weight, l.reps, l.notes, l.performed_at "
            "FROM lifts l JOIN exercises e ON e.id = l.exercise_id "
            "WHERE l.user_name = %s ORDER BY l.performed_at DESC LIMIT %s",
            (user, limit),
        )
    else:
        rows = db.query(
            "SELECT l.id, l.user_name, l.exercise_id, e.name AS exercise, "
            "l.weight, l.reps, l.notes, l.performed_at "
            "FROM lifts l JOIN exercises e ON e.id = l.exercise_id "
            "ORDER BY l.performed_at DESC LIMIT %s",
            (limit,),
        )
    return {
        "sets": [
            {
                "id": r["id"],
                "user": r["user_name"],
                "exercise_id": r["exercise_id"],
                "exercise": r["exercise"],
                "weight": float(r["weight"]),
                "reps": int(r["reps"]),
                "notes": r["notes"],
                "performed_at": r["performed_at"],
            }
            for r in rows
        ]
    }


@app.get("/sets/{set_id}")
def get_set(set_id: int):
    row = db.one(
        "SELECT l.id, l.user_name, l.exercise_id, e.name AS exercise, "
        "l.weight, l.reps, l.notes, l.performed_at "
        "FROM lifts l JOIN exercises e ON e.id = l.exercise_id WHERE l.id = %s",
        (set_id,),
    )
    if not row:
        raise HTTPException(404, f"set {set_id} not found")
    return {
        "id": row["id"],
        "user": row["user_name"],
        "exercise_id": row["exercise_id"],
        "exercise": row["exercise"],
        "weight": float(row["weight"]),
        "reps": int(row["reps"]),
        "notes": row["notes"],
        "performed_at": row["performed_at"],
    }


@app.put("/sets/{set_id}")
def update_set(set_id: int, payload: dict = Body(...)):
    existing = db.one(
        "SELECT l.id, l.user_name, l.exercise_id, e.name AS exercise, "
        "l.weight, l.reps, l.notes, l.performed_at "
        "FROM lifts l JOIN exercises e ON e.id = l.exercise_id WHERE l.id = %s",
        (set_id,),
    )
    if not existing:
        raise HTTPException(404, f"set {set_id} not found")
    user = existing["user_name"]
    ex_id = existing["exercise_id"]

    try:
        weight = float(payload.get("weight", existing["weight"]))
        reps = int(payload.get("reps", existing["reps"]))
        estimates(weight, reps)
    except (TypeError, ValueError) as e:
        raise HTTPException(400, str(e) if isinstance(e, LiftError) else "invalid weight or reps")

    notes = str(payload.get("notes", existing["notes"]))[:200]
    updated = db.one(
        "UPDATE lifts SET weight = %s, reps = %s, notes = %s WHERE id = %s "
        "RETURNING id, user_name, exercise_id, weight, reps, notes, performed_at",
        (weight, reps, notes, set_id),
    )
    for f in FORMULAS:
        cache.drop(f"pb:{user}:{ex_id}:{f}")

    return {
        "id": updated["id"],
        "user": updated["user_name"],
        "exercise": existing["exercise"],
        "weight": float(updated["weight"]),
        "reps": int(updated["reps"]),
        "notes": updated["notes"],
        "performed_at": updated["performed_at"],
    }


@app.delete("/sets/{set_id}")
def delete_set(set_id: int):
    existing = db.one("SELECT id, user_name, exercise_id FROM lifts WHERE id = %s", (set_id,))
    if not existing:
        raise HTTPException(404, f"set {set_id} not found")
    db.query("DELETE FROM lifts WHERE id = %s", (set_id,), fetch=False)
    for f in FORMULAS:
        cache.drop(f"pb:{existing['user_name']}:{existing['exercise_id']}:{f}")
    return {"status": "deleted", "id": set_id}


@app.get("/users/{user}/pbs")
def pbs(user: str, formula: str = DEFAULT_FORMULA):
    if formula not in FORMULAS:
        raise HTTPException(400, f"unknown formula {formula!r}")
    rows = db.query("SELECT DISTINCT e.id, e.name FROM lifts l"
                    " JOIN exercises e ON e.id=l.exercise_id"
                    " WHERE l.user_name=%s ORDER BY e.name", (user,))
    if not rows:
        raise HTTPException(404, "that user has not logged anything")
    out = []
    for r in rows:
        pb = _pb(user, r["id"], formula)
        if pb:
            out.append({"exercise": r["name"], **pb})
    out.sort(key=lambda p: -p["e1rm"])
    return {"user": user, "formula": formula, "personal_bests": out}


@app.get("/users/{user}/exercises/{exercise}/progress")
def progress(user: str, exercise: str, formula: str = DEFAULT_FORMULA):
    if formula not in FORMULAS:
        raise HTTPException(400, f"unknown formula {formula!r}")
    ex = _exercise(exercise)
    rows = db.query("SELECT performed_at::date AS day, weight, reps FROM lifts"
                    " WHERE user_name=%s AND exercise_id=%s"
                    " ORDER BY performed_at", (user, ex["id"]))
    if not rows:
        raise HTTPException(404, "nothing logged for that user and exercise")
    sessions = {}
    for r in rows:
        day = str(r["day"])
        s = sessions.setdefault(day, {"day": day, "sets": []})
        s["sets"].append((float(r["weight"]), int(r["reps"])))
    chart = []
    for day in sorted(sessions):
        sets = sessions[day]["sets"]
        best = max(one_rep_max(w, rp, formula) for w, rp in sets)
        chart.append({"day": day, "sets": len(sets), "best_e1rm": best,
                      "volume": volume(sets),
                      "top_set": max(sets, key=lambda s: one_rep_max(s[0], s[1], formula))})
    stall = plateau([p["best_e1rm"] for p in chart])
    body = {"user": user, "exercise": ex["name"], "formula": formula,
            "sessions": chart, "plateau": stall}
    if stall["stalled"]:
        heaviest = max(float(r["weight"]) for r in rows)
        body["suggestion"] = (
            f"This lift has not moved in {stall['window']} sessions. Drop to "
            f"{deload(heaviest)}kg and build back up, or add a set instead of weight.")
    return body


@app.post("/compare")
def compare_sets(payload: dict = Body(...)):
    try:
        a = {"weight": float(payload["a"]["weight"]), "reps": int(payload["a"]["reps"])}
        b = {"weight": float(payload["b"]["weight"]), "reps": int(payload["b"]["reps"])}
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, 'send {"a":{"weight":100,"reps":5},'
                                 '"b":{"weight":110,"reps":3}}')
    formula = str(payload.get("formula", DEFAULT_FORMULA)).lower()
    if formula not in FORMULAS:
        raise HTTPException(400, f"unknown formula {formula!r} - have {sorted(FORMULAS)}")
    try:
        verdict = compare(a, b, formula)
        agree = formulas_agree(a, b)
    except LiftError as e:
        raise HTTPException(400, str(e))
    winner = verdict["a"] if verdict["better"] == "a" else verdict["b"]
    return {**verdict,
            "formulas_agree": agree,
            "note": ("both formulas rank these the same way" if agree else
                     "Epley and Brzycki disagree about these two sets - the answer "
                     "depends entirely on which one you picked"),
            "verdict": ("too close to call" if verdict["better"] == "tie" else
                        f"{winner['weight']:g}kg x {winner['reps']} is the better set")}


@app.get("/users/{user}/exercises/{exercise}/warmup")
def warmup(user: str, exercise: str):
    ex = _exercise(exercise)
    pb = _pb(user, ex["id"])
    if not pb:
        raise HTTPException(404, "no personal best to warm up to yet")
    return {"user": user, "exercise": ex["name"],
            "working_weight": pb["weight"], "from_pb": pb,
            "ladder": warmup_sets(pb["weight"])}


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Gym Progress Tracker</title>
  <style>
    :root {
      --bg: #0f172a;
      --card: #1e293b;
      --accent: #38bdf8;
      --accent-hover: #0284c7;
      --text: #f8fafc;
      --muted: #94a3b8;
      --border: #334155;
      --success: #22c55e;
      --danger: #ef4444;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      padding: 2rem 1rem;
      max-width: 1100px;
      margin: 0 auto;
    }
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 2rem;
      padding-bottom: 1rem;
      border-bottom: 1px solid var(--border);
    }
    h1 { font-size: 1.8rem; font-weight: 700; color: var(--accent); }
    .badge {
      display: inline-block;
      padding: 0.25rem 0.75rem;
      border-radius: 9999px;
      font-size: 0.85rem;
      font-weight: 600;
      background: #14532d;
      color: #86efac;
    }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; margin-bottom: 2rem; }
    @media (max-width: 768px) { .grid { grid-template-columns: 1fr; } }
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 0.75rem;
      padding: 1.5rem;
    }
    .card h2 { font-size: 1.2rem; margin-bottom: 1rem; color: var(--text); }
    .form-group { margin-bottom: 1rem; }
    label { display: block; font-size: 0.85rem; color: var(--muted); margin-bottom: 0.35rem; }
    input, select, textarea {
      width: 100%;
      padding: 0.6rem 0.8rem;
      background: #0b1120;
      border: 1px solid var(--border);
      border-radius: 0.5rem;
      color: var(--text);
      font-size: 0.95rem;
    }
    input:focus, select:focus, textarea:focus { outline: none; border-color: var(--accent); }
    button {
      background: var(--accent);
      color: #0f172a;
      font-weight: 600;
      padding: 0.6rem 1.2rem;
      border: none;
      border-radius: 0.5rem;
      cursor: pointer;
      transition: background 0.2s;
    }
    button:hover { background: var(--accent-hover); }
    button.btn-danger { background: var(--danger); color: white; padding: 0.3rem 0.6rem; font-size: 0.8rem; }
    button.btn-edit { background: var(--border); color: var(--text); padding: 0.3rem 0.6rem; font-size: 0.8rem; margin-right: 0.25rem; }
    table { width: 100%; border-collapse: collapse; margin-top: 1rem; font-size: 0.9rem; }
    th, td { text-align: left; padding: 0.6rem 0.75rem; border-bottom: 1px solid var(--border); }
    th { color: var(--muted); font-weight: 600; font-size: 0.8rem; text-transform: uppercase; }
    .result-box {
      margin-top: 1rem;
      padding: 0.75rem;
      border-radius: 0.5rem;
      background: #0b1120;
      border: 1px solid var(--border);
      font-family: monospace;
      font-size: 0.85rem;
      white-space: pre-wrap;
      max-height: 200px;
      overflow-y: auto;
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Gym Progress Tracker</h1>
      <p style="color: var(--muted); font-size: 0.9rem;">FastAPI + PostgreSQL + Redis</p>
    </div>
    <div id="statusBadge" class="badge">Checking Health...</div>
  </header>

  <div class="grid">
    <div class="card">
      <h2>Log New Set (POST /sets)</h2>
      <form id="logSetForm">
        <div class="form-group">
          <label>User Name</label>
          <input type="text" id="setUser" value="asha" required>
        </div>
        <div class="form-group">
          <label>Exercise</label>
          <select id="setExercise" required>
            <option value="Bench Press">Bench Press</option>
            <option value="Back Squat">Back Squat</option>
            <option value="Deadlift">Deadlift</option>
            <option value="Overhead Press">Overhead Press</option>
            <option value="Barbell Row">Barbell Row</option>
            <option value="Front Squat">Front Squat</option>
          </select>
        </div>
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem;">
          <div class="form-group">
            <label>Weight (kg)</label>
            <input type="number" step="0.5" id="setWeight" value="65" required>
          </div>
          <div class="form-group">
            <label>Reps</label>
            <input type="number" id="setReps" value="5" min="1" max="12" required>
          </div>
        </div>
        <div class="form-group">
          <label>Notes</label>
          <input type="text" id="setNotes" placeholder="Optional notes">
        </div>
        <button type="submit">Log Set</button>
      </form>
      <div id="logResult" class="result-box" style="display: none;"></div>
    </div>

    <div class="card">
      <h2>Formula Compare (POST /compare)</h2>
      <form id="compareForm">
        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 0.75rem;">
          <fieldset style="border: 1px solid var(--border); padding: 0.75rem; border-radius: 0.5rem;">
            <legend style="color: var(--accent); font-size: 0.85rem; padding: 0 0.25rem;">Set A</legend>
            <label>Weight (kg)</label>
            <input type="number" id="cmpWeightA" value="100" step="0.5" required>
            <label style="margin-top: 0.5rem;">Reps</label>
            <input type="number" id="cmpRepsA" value="5" min="1" max="12" required>
          </fieldset>
          <fieldset style="border: 1px solid var(--border); padding: 0.75rem; border-radius: 0.5rem;">
            <legend style="color: var(--accent); font-size: 0.85rem; padding: 0 0.25rem;">Set B</legend>
            <label>Weight (kg)</label>
            <input type="number" id="cmpWeightB" value="110" step="0.5" required>
            <label style="margin-top: 0.5rem;">Reps</label>
            <input type="number" id="cmpRepsB" value="3" min="1" max="12" required>
          </fieldset>
        </div>
        <div class="form-group" style="margin-top: 1rem;">
          <label>Formula</label>
          <select id="cmpFormula">
            <option value="epley">Epley</option>
            <option value="brzycki">Brzycki</option>
          </select>
        </div>
        <button type="submit">Compare Sets</button>
      </form>
      <div id="compareResult" class="result-box" style="display: none;"></div>
    </div>
  </div>

  <div class="card" style="margin-bottom: 2rem;">
    <div style="display: flex; justify-content: space-between; align-items: center;">
      <h2>Recent Sets (Live CRUD: Read, Update, Delete)</h2>
      <button onclick="loadSets()" style="font-size: 0.8rem; padding: 0.4rem 0.8rem;">Refresh</button>
    </div>
    <div style="overflow-x: auto;">
      <table>
        <thead>
          <tr>
            <th>ID</th>
            <th>User</th>
            <th>Exercise</th>
            <th>Weight</th>
            <th>Reps</th>
            <th>Notes</th>
            <th>Date</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody id="setsTableBody">
          <tr><td colspan="8" style="text-align: center; color: var(--muted);">Loading sets...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <script>
    async function checkHealth() {
      try {
        const res = await fetch('/health');
        const data = await res.json();
        const badge = document.getElementById('statusBadge');
        if (data.status === 'ok' && data.postgres && data.redis) {
          badge.textContent = '● System Healthy (PG + Redis)';
          badge.style.background = '#14532d';
          badge.style.color = '#86efac';
        } else {
          badge.textContent = '▲ Degraded';
          badge.style.background = '#7f1d1d';
          badge.style.color = '#fca5a5';
        }
      } catch (e) {
        document.getElementById('statusBadge').textContent = '✖ Offline';
      }
    }

    async function loadSets() {
      try {
        const res = await fetch('/sets?limit=15');
        const data = await res.json();
        const tbody = document.getElementById('setsTableBody');
        tbody.innerHTML = '';
        if (!data.sets || data.sets.length === 0) {
          tbody.innerHTML = '<tr><td colspan="8" style="text-align: center; color: var(--muted);">No sets logged yet.</td></tr>';
          return;
        }
        data.sets.forEach(s => {
          const tr = document.createElement('tr');
          tr.innerHTML = `
            <td>#${s.id}</td>
            <td><strong>${s.user}</strong></td>
            <td>${s.exercise}</td>
            <td>${s.weight} kg</td>
            <td>${s.reps}</td>
            <td>${s.notes || '-'}</td>
            <td>${s.performed_at ? s.performed_at.substring(0, 10) : ''}</td>
            <td>
              <button class="btn-edit" onclick="editSet(${s.id}, ${s.weight}, ${s.reps}, '${(s.notes || '').replace(/'/g, "\\\\'")}')">Edit</button>
              <button class="btn-danger" onclick="deleteSet(${s.id})">Delete</button>
            </td>
          `;
          tbody.appendChild(tr);
        });
      } catch (e) {
        console.error(e);
      }
    }

    async function deleteSet(id) {
      if (!confirm(`Delete set #${id}?`)) return;
      try {
        const res = await fetch(`/sets/${id}`, { method: 'DELETE' });
        if (res.ok) {
          loadSets();
        } else {
          alert('Failed to delete set');
        }
      } catch (e) {
        alert(e);
      }
    }

    async function editSet(id, curWeight, curReps, curNotes) {
      const weight = prompt('Enter new weight (kg):', curWeight);
      if (weight === null) return;
      const reps = prompt('Enter new reps:', curReps);
      if (reps === null) return;
      const notes = prompt('Enter notes:', curNotes || '');
      if (notes === null) return;

      try {
        const res = await fetch(`/sets/${id}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ weight: parseFloat(weight), reps: parseInt(reps), notes })
        });
        if (res.ok) {
          loadSets();
        } else {
          const err = await res.json();
          alert('Update failed: ' + (err.detail || JSON.stringify(err)));
        }
      } catch (e) {
        alert(e);
      }
    }

    document.getElementById('logSetForm').addEventListener('submit', async (e) => {
      e.preventDefault();
      const payload = {
        user: document.getElementById('setUser').value,
        exercise: document.getElementById('setExercise').value,
        weight: parseFloat(document.getElementById('setWeight').value),
        reps: parseInt(document.getElementById('setReps').value),
        notes: document.getElementById('setNotes').value
      };
      const res = await fetch('/sets', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      const box = document.getElementById('logResult');
      box.style.display = 'block';
      box.textContent = JSON.stringify(data, null, 2);
      loadSets();
    });

    document.getElementById('compareForm').addEventListener('submit', async (e) => {
      e.preventDefault();
      const payload = {
        a: {
          weight: parseFloat(document.getElementById('cmpWeightA').value),
          reps: parseInt(document.getElementById('cmpRepsA').value)
        },
        b: {
          weight: parseFloat(document.getElementById('cmpWeightB').value),
          reps: parseInt(document.getElementById('cmpRepsB').value)
        },
        formula: document.getElementById('cmpFormula').value
      };
      const res = await fetch('/compare', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      const box = document.getElementById('compareResult');
      box.style.display = 'block';
      box.textContent = JSON.stringify(data, null, 2);
    });

    checkHealth();
    loadSets();
  </script>
</body>
</html>"""
