# -*- coding: utf-8 -*-
import sqlite3, os

# Chemin vers la base extraite
db = os.path.join(os.getcwd(), "fusionne_extrait", "userData.db")
conn = sqlite3.connect(db)
cur = conn.cursor()

# 1. Liste des tables
tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table';")]
print("📋 Tables dans la base :", tables)

# 2. PlaylistItem – nombre d’éléments
try:
    cnt = cur.execute("SELECT COUNT(*) FROM PlaylistItem;").fetchone()[0]
    print("🎵 Nombre d’éléments dans PlaylistItem :", cnt)

    # 3. 10 premiers éléments (ID + Label)
    rows = list(cur.execute("SELECT PlaylistItemId, Label FROM PlaylistItem ORDER BY PlaylistItemId LIMIT 10;"))
    print("🔢 10 premiers PlaylistItem (ID, Label) :")
    for row in rows:
        print("   ", row)

except sqlite3.OperationalError as e:
    print("⚠️ Erreur lors de la lecture de PlaylistItem :", e)

conn.close()
