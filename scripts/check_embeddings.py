from pathlib import Path
import sqlite3, struct
import math

from config import DB_PATH, EMBEDDING_DIM

print('DB_PATH from config:', DB_PATH)
print('EMBEDDING_DIM from config:', EMBEDDING_DIM)

db_path = Path(DB_PATH)
if not db_path.exists():
    print('ERROR: DB file not found at', db_path)
    raise SystemExit(1)

conn = sqlite3.connect(db_path)
cur = conn.cursor()
cur.execute('SELECT COUNT(*) FROM chunk_embeddings')
ce_count = cur.fetchone()[0]
cur.execute('SELECT COUNT(*) FROM chunks')
chunks_count = cur.fetchone()[0]
print('chunk_embeddings rows:', ce_count)
print('chunks rows:', chunks_count)

cur.execute('SELECT chunk_id, LENGTH(embedding) FROM chunk_embeddings LIMIT 10')
sample = cur.fetchall()
print('Sample blob lengths (bytes) [up to 10 rows]:')
for row in sample:
    print(' ', row)

expected_bytes = EMBEDDING_DIM * 4
print('Expected bytes per embedding:', expected_bytes)

cur.execute('SELECT DISTINCT LENGTH(embedding) FROM chunk_embeddings')
distinct = [r[0] for r in cur.fetchall()]
print('Distinct embedding byte-lengths (count=%d):' % len(distinct), distinct[:20])

if len(distinct) == 1 and distinct[0] == expected_bytes:
    print('All embeddings have expected byte length.')
else:
    print('Warning: embeddings have varying byte lengths; this may indicate different dims or corrupted rows.')

# deserialize one embedding
cur.execute('SELECT embedding FROM chunk_embeddings LIMIT 1')
row = cur.fetchone()
if row is None:
    print('No embeddings to deserialize.')
else:
    b = row[0]
    n = len(b) // 4
    vec = struct.unpack(f"{n}f", b)
    print('Deserialized vector length:', n)
    norm = math.sqrt(sum(v*v for v in vec))
    print('Sample vector L2 norm:', norm)

conn.close()
