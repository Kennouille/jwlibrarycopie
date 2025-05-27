import sqlite3

db_path = "uploads/merged_userData.db"  # adapte si besoin

with sqlite3.connect(db_path) as conn:
    cursor = conn.cursor()
    cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='Bookmark'")
    schema = cursor.fetchone()[0]
    print("\n=== Schéma de la table Bookmark ===")
    print(schema)
