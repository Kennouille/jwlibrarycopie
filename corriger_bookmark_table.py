import sqlite3

db_path = "uploads/merged_userData.db"  # adapte si besoin

with sqlite3.connect(db_path) as conn:
    cursor = conn.cursor()

    # Étape 1 : sauvegarder les données existantes
    cursor.execute("SELECT LocationId, PublicationLocationId, Slot, Title, Snippet, BlockType, BlockIdentifier FROM Bookmark")
    all_data = cursor.fetchall()
    print(f"Nombre de bookmarks à réinsérer : {len(all_data)}")

    # Étape 2 : supprimer la table Bookmark
    cursor.execute("DROP TABLE IF EXISTS Bookmark")
    print("Ancienne table Bookmark supprimée.")

    # Étape 3 : recréer la table avec AUTOINCREMENT
    cursor.execute("""
        CREATE TABLE Bookmark(
            BookmarkId              INTEGER PRIMARY KEY AUTOINCREMENT,
            LocationId              INTEGER NOT NULL,
            PublicationLocationId   INTEGER NOT NULL,
            Slot                    INTEGER NOT NULL,
            Title                   TEXT NOT NULL,
            Snippet                 TEXT,
            BlockType               INTEGER NOT NULL DEFAULT 0,
            BlockIdentifier         INTEGER,
            FOREIGN KEY(LocationId) REFERENCES Location(LocationId),
            FOREIGN KEY(PublicationLocationId) REFERENCES Location(LocationId),
            CONSTRAINT PublicationLocationId_Slot UNIQUE (PublicationLocationId, Slot),
            CHECK((BlockType = 0 AND BlockIdentifier IS NULL) OR((BlockType BETWEEN 1 AND 2) AND BlockIdentifier IS NOT NULL))
        )
    """)
    print("Nouvelle table Bookmark créée avec AUTOINCREMENT.")

    # Étape 4 : réinsertion des données sans fournir BookmarkId
    for row in all_data:
        cursor.execute("""
            INSERT INTO Bookmark (LocationId, PublicationLocationId, Slot, Title, Snippet, BlockType, BlockIdentifier)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, row)

    conn.commit()
    print("✅ Données réinsérées avec succès.")
