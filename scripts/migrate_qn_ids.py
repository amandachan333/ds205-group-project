import hashlib
import sqlite3

def stable_qid(text: str) -> str:
    norm = " ".join(text.split()).lower()
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]

conn = sqlite3.connect("db/benchmark.db")
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA foreign_keys = OFF")

# Read all current questions and group by canonical id
rows = conn.execute("SELECT question_id, question_text FROM questions").fetchall()
print(f"Before: {len(rows)} question rows")

groups: dict[str, list[str]] = {}
canonical_text: dict[str, str] = {}
for r in rows:
    cid = stable_qid(r["question_text"])
    groups.setdefault(cid, []).append(r["question_id"])
    canonical_text[cid] = r["question_text"]

print(f"Will collapse to {len(groups)} canonical questions")

# For each group: ensure canonical row exists, re-point runs, delete duplicates
for cid, old_ids in groups.items():
    conn.execute(
        "INSERT OR IGNORE INTO questions (question_id, question_text) VALUES (?, ?)",
        (cid, canonical_text[cid]),
    )
    placeholders = ",".join("?" * len(old_ids))
    conn.execute(
        f"UPDATE runs SET question_id = ? WHERE question_id IN ({placeholders})",
        (cid, *old_ids),
    )
    to_delete = [oid for oid in old_ids if oid != cid]
    if to_delete:
        placeholders = ",".join("?" * len(to_delete))
        conn.execute(
            f"DELETE FROM questions WHERE question_id IN ({placeholders})",
            tuple(to_delete),
        )

conn.commit()
conn.execute("PRAGMA foreign_keys = ON")

nq = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
nr = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
print(f"After: {nq} question rows, {nr} runs rows")
conn.close()