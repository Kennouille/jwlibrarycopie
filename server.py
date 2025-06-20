from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
from datetime import datetime
import os
import zipfile
import sqlite3
import shutil
import uuid
import time
import sys
import gc
import io
import traceback
import threading


app = Flask(__name__)
app.config['PROPAGATE_EXCEPTIONS'] = True
CORS(app, origins=["https://jwlibrarycopie.netlify.app"])

UPLOAD_FOLDER = "uploads"
EXTRACT_FOLDER = "extracted"

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(EXTRACT_FOLDER, exist_ok=True)


def normalize_mapping_keys(mapping):
    return {
        (os.path.normpath(k[0]), k[1]): v
        for k, v in mapping.items()
    }


def get_current_local_iso8601():
    now_local = datetime.datetime.now()
    return now_local.strftime("%Y-%m-%dT%H:%M:%S")


def checkpoint_db(db_path):
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("PRAGMA wal_checkpoint(FULL)")
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Erreur lors du checkpoint de {db_path}: {e}")


def list_tables(db_path):
    """
    Retourne une liste des noms de tables présentes dans la base de données
    spécifiée par 'db_path', en excluant les tables système (commençant par 'sqlite_').
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT name
        FROM sqlite_master
        WHERE type='table'
          AND name NOT LIKE 'sqlite_%'
    """)
    tables = [row[0] for row in cursor.fetchall()]
    conn.close()
    return tables


def merge_independent_media(merged_db_path, file1_db, file2_db):
    """
    Fusionne la table IndependentMedia des deux bases sources dans la base fusionnée.
    Deux lignes sont considérées identiques si (OriginalFilename, FilePath, Hash) sont identiques.
    Si une ligne existe déjà, on ignore la nouvelle pour préserver les données existantes.
    Retourne un mapping : {(db_source, ancien_ID) : nouveau_ID, ...}
    """
    print("\n[FUSION INDEPENDENTMEDIA]")
    mapping = {}
    with sqlite3.connect(merged_db_path) as merged_conn:
        merged_cursor = merged_conn.cursor()

        for db_path in [file1_db, file2_db]:
            print(f"Traitement de {db_path}")
            with sqlite3.connect(db_path) as src_conn:
                src_cursor = src_conn.cursor()
                src_cursor.execute("""
                    SELECT IndependentMediaId, OriginalFilename, FilePath, MimeType, Hash
                    FROM IndependentMedia
                """)
                rows = src_cursor.fetchall()

                for row in rows:
                    old_id, orig_fn, file_path, mime, hash_val = row
                    print(f"  - Média : {orig_fn}, Hash={hash_val}")

                    # Vérifie si la ligne existe déjà (évite doublons)
                    merged_cursor.execute("""
                        SELECT IndependentMediaId, MimeType
                        FROM IndependentMedia
                        WHERE OriginalFilename = ? AND FilePath = ? AND Hash = ?
                    """, (orig_fn, file_path, hash_val))
                    result = merged_cursor.fetchone()

                    if result:
                        new_id, existing_mime = result
                        # Au lieu de mettre à jour le MimeType, on ignore simplement la nouvelle ligne
                        print(f"    > Ligne déjà présente pour ID {new_id} (ignorée pour {db_path})")
                    else:
                        merged_cursor.execute("""
                            INSERT INTO IndependentMedia (OriginalFilename, FilePath, MimeType, Hash)
                            VALUES (?, ?, ?, ?)
                        """, (orig_fn, file_path, mime, hash_val))
                        new_id = merged_cursor.lastrowid
                        print(f"    > Insertion nouvelle ligne ID {new_id}")

                    mapping[(db_path, old_id)] = new_id

        merged_conn.commit()

    print("Fusion IndependentMedia terminée.", flush=True)
    return mapping


def read_notes_and_highlights(db_path):
    if not os.path.exists(db_path):
        return {"error": f"Base de données introuvable : {db_path}"}
    checkpoint_db(db_path)
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT n.NoteId, n.Guid, n.Title, n.Content, n.LocationId, um.UserMarkGuid,
               n.LastModified, n.Created, n.BlockType, n.BlockIdentifier
        FROM Note n
        LEFT JOIN UserMark um ON n.UserMarkId = um.UserMarkId
    """)
    notes = cursor.fetchall()

    cursor.execute("""
        SELECT UserMarkId, ColorIndex, LocationId, StyleIndex, UserMarkGuid, Version
        FROM UserMark
    """)
    highlights = cursor.fetchall()

    conn.close()
    return {"notes": notes, "highlights": highlights}


def generate_preview_data(file1_db, file2_db):
    return {
        "notes": compare_notes_with_preview(file1_db, file2_db),
        "bookmarks": compare_bookmarks_with_preview(file1_db, file2_db),
        "tags": compare_tags_with_preview(file1_db, file2_db)
    }


@app.route('/prepare-preview', methods=['POST'])
def prepare_preview():
    try:
        file1_path = os.path.join("extracted", "file1_extracted", "userData.db")
        file2_path = os.path.join("extracted", "file2_extracted", "userData.db")

        if not os.path.exists(file1_path) or not os.path.exists(file2_path):
            return jsonify({"error": "Fichiers non trouvés"}), 400

        conn1 = sqlite3.connect(file1_path)
        conn2 = sqlite3.connect(file2_path)

        def extract_table(conn, table_name):
            cursor = conn.cursor()
            cursor.execute(f"SELECT * FROM {table_name}")
            columns = [desc[0] for desc in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

        # Tu extrais chaque table
        notes1 = extract_table(conn1, "Note")
        notes2 = extract_table(conn2, "Note")
        bookmarks1 = extract_table(conn1, "Bookmark")
        bookmarks2 = extract_table(conn2, "Bookmark")
        tags1 = extract_table(conn1, "Tag")
        tags2 = extract_table(conn2, "Tag")

        # Extraire TagMap pour chaque fichier (aucun filtre appliqué)
        tagmaps1_all = extract_table(conn1, "TagMap")
        tagmaps1 = tagmaps1_all  # on garde tout
        tagmaps2_all = extract_table(conn2, "TagMap")
        tagmaps2 = tagmaps2_all  # on garde tout

        conn1.close()
        conn2.close()

        # Tu renvoies les données sous la bonne structure
        response = {
            "notes": {
                "file1": notes1,
                "file2": notes2
            },
            "bookmarks": {
                "file1": bookmarks1,
                "file2": bookmarks2
            },
            "tags": {
                "file1": tags1,
                "file2": tags2
            },
            "tagMaps": {
                "file1": tagmaps1,
                "file2": tagmaps2
            }
        }

        return jsonify(response)

    except Exception as e:
        print("Erreur /prepare-preview:", str(e))
        return jsonify({"error": str(e)}), 500


def compare_notes_with_preview(file1_db, file2_db):
    import sqlite3

    conn1 = sqlite3.connect(file1_db)
    conn2 = sqlite3.connect(file2_db)
    cur1 = conn1.cursor()
    cur2 = conn2.cursor()

    cur1.execute("SELECT NoteGUID, Title, Content, LastModified FROM Note")
    notes1 = {row[0]: row[1:] for row in cur1.fetchall()}

    cur2.execute("SELECT NoteGUID, Title, Content, LastModified FROM Note")
    notes2 = {row[0]: row[1:] for row in cur2.fetchall()}

    all_guids = set(notes1.keys()) | set(notes2.keys())
    results = []

    for guid in all_guids:
        n1 = notes1.get(guid)
        n2 = notes2.get(guid)

        def dictify(row):
            if not row:
                return None
            return {
                "title": row[0],
                "content": row[1],
                "lastModified": row[2]
            }

        merged = None
        status = "identical"
        default = None

        if n1 and n2:
            status = "identical" if n1 == n2 else "different"
            default = "file2" if n2[2] > n1[2] else "file1"
            merged = dictify(n2) if default == "file2" else dictify(n1)
        elif n1:
            status = "only_file1"
            default = "file1"
            merged = dictify(n1)
        elif n2:
            status = "only_file2"
            default = "file2"
            merged = dictify(n2)

        results.append({
            "guid": guid,
            "file1": dictify(n1),
            "file2": dictify(n2),
            "merged": merged,
            "status": status,
            "defaultChoice": default
        })

    conn1.close()
    conn2.close()
    return results


def compare_bookmarks_with_preview(file1_db, file2_db):
    conn1 = sqlite3.connect(file1_db)
    conn2 = sqlite3.connect(file2_db)
    cur1 = conn1.cursor()
    cur2 = conn2.cursor()

    cur1.execute("SELECT BookmarkId, LocationId, Title FROM Bookmark")
    bookmarks1 = {row[0]: row[1:] for row in cur1.fetchall()}

    cur2.execute("SELECT BookmarkId, LocationId, Title FROM Bookmark")
    bookmarks2 = {row[0]: row[1:] for row in cur2.fetchall()}

    all_ids = set(bookmarks1.keys()) | set(bookmarks2.keys())
    results = []

    for bid in all_ids:
        b1 = bookmarks1.get(bid)
        b2 = bookmarks2.get(bid)

        def dictify(b):
            if not b:
                return None
            return {
                "locationId": b[0],
                "title": b[1]
            }

        status = "identical"
        default = None
        merged = None

        if b1 and b2:
            status = "identical" if b1 == b2 else "different"
            default = "file2"
            merged = dictify(b2)
        elif b1:
            status = "only_file1"
            default = "file1"
            merged = dictify(b1)
        elif b2:
            status = "only_file2"
            default = "file2"
            merged = dictify(b2)

        results.append({
            "id": bid,
            "file1": dictify(b1),
            "file2": dictify(b2),
            "merged": merged,
            "status": status,
            "defaultChoice": default
        })

    conn1.close()
    conn2.close()
    return results


def compare_tags_with_preview(file1_db, file2_db):
    conn1 = sqlite3.connect(file1_db)
    conn2 = sqlite3.connect(file2_db)
    cur1 = conn1.cursor()
    cur2 = conn2.cursor()

    cur1.execute("SELECT TagId, Name FROM Tag")
    tags1 = {row[0]: row[1] for row in cur1.fetchall()}

    cur2.execute("SELECT TagId, Name FROM Tag")
    tags2 = {row[0]: row[1] for row in cur2.fetchall()}

    all_ids = set(tags1.keys()) | set(tags2.keys())
    results = []

    for tid in all_ids:
        name1 = tags1.get(tid)
        name2 = tags2.get(tid)

        status = "identical"
        default = None
        merged = None

        if name1 and name2:
            status = "identical" if name1 == name2 else "different"
            default = "file2"
            merged = name2
        elif name1:
            status = "only_file1"
            default = "file1"
            merged = name1
        elif name2:
            status = "only_file2"
            default = "file2"
            merged = name2

        results.append({
            "id": tid,
            "file1": { "name": name1 } if name1 else None,
            "file2": { "name": name2 } if name2 else None,
            "merged": { "name": merged } if merged else None,
            "status": status,
            "defaultChoice": default
        })

    conn1.close()
    conn2.close()
    return results


@app.route('/preview-merge', methods=['GET'])
def preview_merge():
    file1 = os.path.join(EXTRACT_FOLDER, "file1_extracted", "userData.db")
    file2 = os.path.join(EXTRACT_FOLDER, "file2_extracted", "userData.db")

    if not os.path.exists(file1) or not os.path.exists(file2):
        return jsonify({"error": "Fichiers source manquants"}), 400

    preview_data = generate_preview_data(file1, file2)
    return jsonify(preview_data), 200


def extract_file(file_path, extract_folder):
    zip_path = file_path.replace(".jwlibrary", ".zip")
    if os.path.exists(zip_path):
        os.remove(zip_path)
    os.rename(file_path, zip_path)
    extract_full_path = os.path.join(EXTRACT_FOLDER, extract_folder)
    os.makedirs(extract_full_path, exist_ok=True)
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        zip_ref.extractall(extract_full_path)
    return extract_full_path


def create_merged_schema(merged_db_path, base_db_path):
    checkpoint_db(base_db_path)
    src_conn = sqlite3.connect(base_db_path)
    src_cursor = src_conn.cursor()
    src_cursor.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE type IN ('table', 'index', 'trigger', 'view') "
        "AND name NOT LIKE 'sqlite_%'"
    )
    schema_items = src_cursor.fetchall()
    src_conn.close()

    merged_conn = sqlite3.connect(merged_db_path)
    merged_cursor = merged_conn.cursor()
    for obj_type, name, sql in schema_items:
        # On exclut la table (et triggers associés) LastModified
        if (obj_type == 'table' and name == "LastModified") or (obj_type == 'trigger' and "LastModified" in sql):
            continue
        if sql:
            try:
                merged_cursor.execute(sql)
            except Exception as e:
                print(f"Erreur lors de la création de {obj_type} '{name}': {e}")
    merged_conn.commit()

    try:
        merged_cursor.execute("DROP TABLE IF EXISTS LastModified")
        merged_cursor.execute("CREATE TABLE LastModified (LastModified TEXT NOT NULL)")
    except Exception as e:
        print(f"Erreur lors de la création de la table LastModified: {e}")
    merged_conn.commit()

    # Création correcte de PlaylistItemMediaMap si elle n'existe pas
    merged_cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='PlaylistItemMediaMap'"
    )
    if not merged_cursor.fetchone():
        merged_cursor.execute("""
            CREATE TABLE PlaylistItemMediaMap (
                PlaylistItemId   INTEGER NOT NULL,
                MediaFileId      INTEGER NOT NULL,
                OrderIndex       INTEGER NOT NULL,
                PRIMARY KEY (PlaylistItemId, MediaFileId),
                FOREIGN KEY (PlaylistItemId) REFERENCES PlaylistItem(PlaylistItemId),
                FOREIGN KEY (MediaFileId)  REFERENCES IndependentMedia(IndependentMediaId)
            )
        """)
        print("PlaylistItemMediaMap (avec MediaFileId, OrderIndex) créée dans la base fusionnée.")

    merged_conn.commit()
    merged_conn.close()


def create_table_if_missing(merged_conn, source_db_paths, table):
    cursor = merged_conn.cursor()
    cursor.execute(f"PRAGMA table_info({table})")
    if cursor.fetchone() is None:
        create_sql = None
        for db_path in source_db_paths:
            checkpoint_db(db_path)
            src_conn = sqlite3.connect(db_path)
            src_cursor = src_conn.cursor()
            src_cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,))
            row = src_cursor.fetchone()
            src_conn.close()
            if row and row[0]:
                create_sql = row[0]
                break
        if create_sql:
            try:
                merged_conn.execute(create_sql)
                print(f"Table {table} créée dans la base fusionnée.")
            except Exception as e:
                print(f"Erreur lors de la création de {table}: {e}")
        else:
            print(f"Aucun schéma trouvé pour la table {table} dans les bases sources.")


def merge_other_tables(merged_db_path, db1_path, db2_path, exclude_tables=None):
    """
    Fusionne toutes les tables restantes (hors celles spécifiées dans exclude_tables)
    dans la base fusionnée de manière idempotente.
    Pour chaque table, la fonction vérifie si une ligne identique (comparaison sur toutes
    les colonnes sauf la clé primaire) est déjà présente avant insertion.
    """
    if exclude_tables is None:
        exclude_tables = ["Note", "UserMark", "Bookmark", "InputField"]

    # On effectue un checkpoint pour s'assurer que les données sont bien synchronisées.
    checkpoint_db(db1_path)
    checkpoint_db(db2_path)

    def get_tables(path):
        with sqlite3.connect(path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
            return {row[0] for row in cursor.fetchall()}

    tables1 = get_tables(db1_path)
    tables2 = get_tables(db2_path)
    all_tables = (tables1 | tables2) - set(exclude_tables)

    merged_conn = sqlite3.connect(merged_db_path)
    merged_cursor = merged_conn.cursor()
    source_db_paths = [db1_path, db2_path]

    for table in all_tables:
        # Crée la table dans la DB fusionnée si elle est manquante
        create_table_if_missing(merged_conn, source_db_paths, table)
        merged_cursor.execute(f"PRAGMA table_info({table})")
        columns_info = merged_cursor.fetchall()
        if not columns_info:
            print(f"❌ Table {table} introuvable dans la DB fusionnée.")
            continue

        # 🔵 Sécurisation forte : tout est string forcée
        columns = [str(col[1]) for col in columns_info]
        columns_joined = ", ".join(columns)
        placeholders = ", ".join(["?"] * len(columns))

        for source_path in source_db_paths:
            with sqlite3.connect(source_path) as src_conn:
                src_cursor = src_conn.cursor()
                try:
                    src_cursor.execute(f"SELECT * FROM {table}")
                    rows = src_cursor.fetchall()
                except Exception as e:
                    print(f"⚠️ Erreur lecture de {table} depuis {source_path}: {e}")
                    rows = []

                for row in rows:
                    if len(columns) > 1:
                        where_clause = " AND ".join([f"{str(col)}=?" for col in columns[1:]])
                        check_query = f"SELECT 1 FROM {table} WHERE {where_clause} LIMIT 1"
                        merged_cursor.execute(check_query, row[1:])
                        exists = merged_cursor.fetchone()
                    else:
                        # Cas spécial : table avec seulement clé primaire
                        exists = None

                    if not exists:
                        cur_max = merged_cursor.execute(f"SELECT MAX({str(columns[0])}) FROM {table}").fetchone()[
                                      0] or 0
                        new_id = int(cur_max) + 1
                        new_row = (new_id,) + row[1:]
                        print(f"✅ INSERT dans {table} depuis {source_path}: {new_row}")
                        merged_cursor.execute(
                            f"INSERT INTO {table} ({columns_joined}) VALUES ({placeholders})", new_row
                        )
                    else:
                        print(f"⏩ Doublon ignoré dans {table} depuis {source_path}: {row[1:]}")

    merged_conn.commit()
    merged_conn.close()


def merge_bookmarks(merged_db_path, file1_db, file2_db, location_id_map, bookmark_choices):
    print("\n[FUSION BOOKMARKS AVEC CHOIX UTILISATEUR]")
    mapping = {}
    conn = sqlite3.connect(merged_db_path)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS MergeMapping_Bookmark (
            SourceDb TEXT,
            OldID INTEGER,
            NewID INTEGER,
            PRIMARY KEY (SourceDb, OldID)
        )
    """)

    def fetch_bookmarks(db_path):
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT BookmarkId, LocationId, PublicationLocationId, Slot, Title, Snippet, BlockType, BlockIdentifier FROM Bookmark")
            return cur.fetchall()

    bookmarks1 = fetch_bookmarks(file1_db)
    bookmarks2 = fetch_bookmarks(file2_db)

    for frontend_index_key, choice_data in bookmark_choices.items():  # Note: j'ai renommé `key` en `frontend_index_key` pour la clarté
        if not isinstance(choice_data, dict):
            print(f"⚠️ Données de choix de marque-page inattendues pour {frontend_index_key}: {choice_data}",
                  flush=True)
            continue

        choice = choice_data.get("choice", "both")  # Défaut à "both" ou "file1" selon votre préférence
        edited_data = choice_data.get("edited", {})  # Dictionnaire { "file1": {Title: ...}, "file2": {Title: ...} }
        bookmark_ids_frontend = choice_data.get("bookmarkIds", {})  # Dictionnaire { "file1": OldID1, "file2": OldID2 }

        # Déterminer la source de la note à insérer (et son ancien ID)
        bookmark_to_process = None  # C'est l'objet Bookmark qui sera inséré
        source_db_origin = None  # 'file1_db' ou 'file2_db'
        original_bookmark_id = None  # L'ancien BookmarkId du fichier source

        # 1. Gérer le cas 'ignore'
        if choice == "ignore":
            print(f"⏩ Bookmark (frontend_index: {frontend_index_key}) ignoré par choix utilisateur.", flush=True)
            continue

        # 2. Gérer les choix 'file1', 'file2', 'both'
        if choice == "file1":
            old_id_f1 = bookmark_ids_frontend.get("file1")
            # Assurez-vous que vous avez une fonction pour récupérer les données originales par ID si nécessaire,
            # ou que bookmarks1 est une map.
            original_bookmark = next((b for b in bookmarks1 if b[0] == old_id_f1), None)
            if original_bookmark:
                bookmark_to_process = list(original_bookmark)  # Convertir en liste mutable
                source_db_origin = file1_db
                original_bookmark_id = old_id_f1
        elif choice == "file2":
            old_id_f2 = bookmark_ids_frontend.get("file2")
            original_bookmark = next((b for b in bookmarks2 if b[0] == old_id_f2), None)
            if original_bookmark:
                bookmark_to_process = list(original_bookmark)
                source_db_origin = file2_db
                original_bookmark_id = old_id_f2
        elif choice == "both":
            # Pour "both", la logique peut être plus complexe si vous voulez fusionner les contenus.
            # Cependant, votre frontend envoie déjà `edited.file1` et `edited.file2`.
            # Nous allons privilégier la version de file1 si elle existe, sinon celle de file2,
            # puis appliquer les edits.
            old_id_f1 = bookmark_ids_frontend.get("file1")
            old_id_f2 = bookmark_ids_frontend.get("file2")

            original_bookmark_f1 = next((b for b in bookmarks1 if b[0] == old_id_f1), None)
            original_bookmark_f2 = next((b for b in bookmarks2 if b[0] == old_id_f2), None)

            if original_bookmark_f1:
                bookmark_to_process = list(original_bookmark_f1)
                source_db_origin = file1_db  # Considérons file1 comme la source principale pour le remappage d'ID
                original_bookmark_id = old_id_f1
            elif original_bookmark_f2:  # Cas où "both" est choisi mais la note n'existe que dans file2
                bookmark_to_process = list(original_bookmark_f2)
                source_db_origin = file2_db  # La source est file2
                original_bookmark_id = old_id_f2
            else:
                print(f"⚠️ Choix 'both' pour Bookmark {frontend_index_key} mais aucun ID de bookmark trouvé. Ignoré.",
                      flush=True)
                continue  # Ne rien faire si aucun bookmark n'est trouvé pour 'both'

        if not bookmark_to_process:
            print(
                f"⚠️ Bookmark {frontend_index_key} (choix: {choice}) introuvable dans les données originales. Ignoré.",
                flush=True)
            continue

        # 3. Appliquer les modifications éditées par l'utilisateur
        # Les index des colonnes Bookmark: 0:BookmarkId, 1:LocationId, 2:PublicationLocationId, 3:Slot, 4:Title, 5:Snippet, 6:BlockType, 7:BlockIdentifier

        # Récupérez les données éditées pour la source correspondante (file1 ou file2)
        # Si le choix est "both", nous appliquons les edits de la source privilégiée (file1 si existe, sinon file2).
        # C'est une simplification. Pour une vraie fusion de contenu 'both', il faudrait une logique plus complexe.
        edited_source_key = "file1" if source_db_origin == file1_db else "file2"
        current_edited_data = edited_data.get(edited_source_key, {})

        # Appliquer le Title édité
        bookmark_to_process[4] = current_edited_data.get("Title", bookmark_to_process[4])  # index 4 est Title

        # Re-extraire les données avec les modifications appliquées
        old_id, loc_id, pub_loc_id, slot, title, snippet, block_type, block_id = bookmark_to_process

        # Nouveau LocationId mappé
        new_loc_id = location_id_map.get((source_db_origin, loc_id), loc_id)
        new_pub_loc_id = location_id_map.get((source_db_origin, pub_loc_id), pub_loc_id)

        # Vérification des LocationId après remappage
        cursor.execute("SELECT 1 FROM Location WHERE LocationId IN (?, ?)", (new_loc_id, new_pub_loc_id))
        if len(cursor.fetchall()) != 2:
            print(
                f"⚠️ LocationId introuvable pour Bookmark OldID {old_id} dans {source_db_origin} (LocationId {new_loc_id} ou PublicationLocationId {new_pub_loc_id}), ignoré.",
                flush=True)
            continue

        # Déjà fusionné ?
        # Si un bookmark avec le même old_id de la même source a déjà été inséré/mappé
        cursor.execute("""
                SELECT NewID FROM MergeMapping_Bookmark
                WHERE SourceDb = ? AND OldID = ?
            """, (source_db_origin, old_id))
        res = cursor.fetchone()
        if res:
            mapping[(source_db_origin, old_id)] = res[0]
            print(f"⏩ Bookmark OldID {old_id} de {os.path.basename(source_db_origin)} déjà mappé à NewID {res[0]}",
                  flush=True)
            continue

        # Vérification de doublon pour le NOUVEAU bookmark (après edits et remappage de LocationId)
        cursor.execute("""
                SELECT BookmarkId FROM Bookmark
                WHERE LocationId = ?
                AND PublicationLocationId = ?
                AND Slot = ?
                AND Title = ?
                AND IFNULL(Snippet, '') = IFNULL(?, '')
                AND BlockType = ?
                AND IFNULL(BlockIdentifier, -1) = IFNULL(?, -1)
            """, (new_loc_id, new_pub_loc_id, slot, title, snippet, block_type, block_id))
        existing = cursor.fetchone()

        if existing:
            existing_id = existing[0]
            print(
                f"⏩ Bookmark identique trouvé (après édition): OldID {old_id} de {os.path.basename(source_db_origin)} → NewID {existing_id}",
                flush=True)
            mapping[(source_db_origin, old_id)] = existing_id
            cursor.execute("""
                    INSERT OR IGNORE INTO MergeMapping_Bookmark (SourceDb, OldID, NewID)
                    VALUES (?, ?, ?)
                """, (source_db_origin, old_id, existing_id))
            continue

        # Ajustement de slot (si nécessaire)
        original_slot = slot
        current_slot = slot
        while True:
            cursor.execute("""
                    SELECT 1 FROM Bookmark
                    WHERE PublicationLocationId = ? AND Slot = ?
                """, (new_pub_loc_id, current_slot))
            if not cursor.fetchone():
                break
            current_slot += 1
        slot = current_slot  # Mettez à jour le slot avec le slot libre trouvé

        print(
            f"Insertion Bookmark: OldID {old_id} de {os.path.basename(source_db_origin)} (slot {original_slot} -> {slot}), PubLocId {new_pub_loc_id}, Title='{title}'",
            flush=True)
        cursor.execute("""
                INSERT INTO Bookmark
                (LocationId, PublicationLocationId, Slot, Title,
                 Snippet, BlockType, BlockIdentifier)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (new_loc_id, new_pub_loc_id, slot, title, snippet, block_type, block_id))
        new_id = cursor.lastrowid
        mapping[(source_db_origin, old_id)] = new_id

        cursor.execute("""
                INSERT INTO MergeMapping_Bookmark (SourceDb, OldID, NewID)
                VALUES (?, ?, ?)
            """, (source_db_origin, old_id, new_id))

    conn.commit()
    conn.close()
    print("✔ Fusion Bookmarks terminée (avec choix utilisateur).", flush=True)
    return mapping


def merge_notes(merged_db_path, db1_path, db2_path, location_id_map, usermark_guid_map, note_choices, tag_id_map):
    print("\n=== FUSION DES NOTES AVEC CHOIX UTILISATEUR ===")
    inserted = 0
    note_mapping = {}

    def fetch_notes(db_path):
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT n.NoteId, n.Guid, n.UserMarkId, um.UserMarkGuid, n.LocationId,
                       n.Title, n.Content, n.LastModified, n.Created, n.BlockType, n.BlockIdentifier
                FROM Note n
                LEFT JOIN UserMark um ON n.UserMarkId = um.UserMarkId
            """)
            rows = []
            for row in cur.fetchall():
                note_id, guid, usermark_id, usermark_guid, location_id, title, content, lastmod, created, block_type, block_ident = row
                if usermark_guid is None and usermark_id is not None:
                    cur2 = conn.cursor()
                    cur2.execute("SELECT UserMarkGuid FROM UserMark WHERE UserMarkId = ?", (usermark_id,))
                    result = cur2.fetchone()
                    usermark_guid = result[0] if result else None
                rows.append((note_id, guid, usermark_guid, location_id, title, content, lastmod, created, block_type,
                             block_ident))
            return rows

    notes1 = fetch_notes(db1_path)
    notes2 = fetch_notes(db2_path)

    # Convertir les listes de notes en dictionnaires pour un accès plus rapide par NoteId
    notes1_dict = {note[0]: note for note in notes1}
    notes2_dict = {note[0]: note for note in notes2}

    conn = sqlite3.connect(merged_db_path)
    cursor = conn.cursor()

    # --- 1) Fusion via choix utilisateur ---
    # Cette boucle va maintenant être la source principale de vérité pour les notes à insérer
    for key, choice_data in note_choices.items():
        if not isinstance(choice_data, dict):
            print(f"⚠️ Données de choix de note inattendues pour {key}: {choice_data}", flush=True)
            continue

        choice = choice_data.get("choice", "both")
        edited_content = choice_data.get("edited",
                                         {})  # Ce dictionnaire contient {"file1": {Title: ..., Content: ...}, "file2": {...}}
        note_ids_from_frontend = choice_data.get("noteIds",
                                                 {})  # Contient {"file1": old_id_file1, "file2": old_id_file2}

        # Si le choix est d'ignorer la note, passez
        if choice == "ignore":
            print(f"⏩ Note (frontend_key: {key}) ignorée par choix utilisateur.", flush=True)
            continue

        # Déterminer quelle note originale utiliser comme base, puis appliquer les edits
        original_note_data_tuple = None  # Le tuple de la note originale (comme fetch_notes le retourne)
        source_db_for_mapping = None  # La DB d'origine (db1_path ou db2_path)
        original_note_id = None  # L'ID de la note dans sa DB d'origine

        if choice == "file1":
            old_id_f1 = note_ids_from_frontend.get("file1")
            if old_id_f1 in notes1_dict:
                original_note_data_tuple = notes1_dict[old_id_f1]
                source_db_for_mapping = db1_path
                original_note_id = old_id_f1
            else:
                print(
                    f"⚠️ Choix 'file1' pour note {key} mais NoteId {old_id_f1} introuvable dans {os.path.basename(db1_path)}. Ignoré.",
                    flush=True)
                continue

        elif choice == "file2":
            old_id_f2 = note_ids_from_frontend.get("file2")
            if old_id_f2 in notes2_dict:
                original_note_data_tuple = notes2_dict[old_id_f2]
                source_db_for_mapping = db2_path
                original_note_id = old_id_f2
            else:
                print(
                    f"⚠️ Choix 'file2' pour note {key} mais NoteId {old_id_f2} introuvable dans {os.path.basename(db2_path)}. Ignoré.",
                    flush=True)
                continue

        elif choice == "both":
            # Pour "both", nous allons privilégier la note de file1 comme base si elle existe.
            # Le frontend est censé avoir déjà combiné les informations dans `edited`.
            old_id_f1 = note_ids_from_frontend.get("file1")
            old_id_f2 = note_ids_from_frontend.get("file2")

            if old_id_f1 in notes1_dict:
                original_note_data_tuple = notes1_dict[old_id_f1]
                source_db_for_mapping = db1_path
                original_note_id = old_id_f1
            elif old_id_f2 in notes2_dict:
                original_note_data_tuple = notes2_dict[old_id_f2]
                source_db_for_mapping = db2_path
                original_note_id = old_id_f2
            else:
                print(f"⚠️ Choix 'both' pour note {key} mais aucun ID de note trouvé dans les sources. Ignoré.",
                      flush=True)
                continue

        # Si nous avons une note à traiter (c-à-d, non "ignore" et trouvée dans les DB sources)
        if original_note_data_tuple:
            # Déballez les données de la note originale
            old_note_id, guid, usermark_guid, location_id, title, content, last_modified, created, block_type, block_identifier = original_note_data_tuple

            # --- APPLIQUER LES MODIFICATIONS "EDITED" DU FRONTEND ICI ---
            # Le `source_key` dépend de la note originale que nous avons choisie comme base
            source_key_for_edited = "file1" if os.path.normpath(source_db_for_mapping) == os.path.normpath(
                db1_path) else "file2"

            # Appliquer le titre édité (si présent)
            title = edited_content.get(source_key_for_edited, {}).get("Title", title)
            # Appliquer le contenu édité (si présent)
            content = edited_content.get(source_key_for_edited, {}).get("Content", content)
            # -------------------------------------------------------------

            # normaliser location & usermark
            norm_map = {(os.path.normpath(k[0]), k[1]): v for k, v in location_id_map.items()}
            new_loc = norm_map.get((os.path.normpath(source_db_for_mapping), location_id)) if location_id else None
            new_um = usermark_guid_map.get(usermark_guid) if usermark_guid else None

            if new_loc is None:
                print(
                    f"⚠️ LocationId {location_id} de note {original_note_id} de {os.path.basename(source_db_for_mapping)} non mappé, ignoré.",
                    flush=True)
                continue

            # existence check (votre logique de GUID existante, inchangée)
            cursor.execute("SELECT NoteId, Title, Content FROM Note WHERE Guid = ?", (guid,))
            existing = cursor.fetchone()
            if existing and existing[1] == title and existing[2] == content:
                note_mapping[(source_db_for_mapping, original_note_id)] = existing[0]
                print(
                    f"⏩ Note identique trouvée (GUID: {guid}, OldID: {original_note_id} de {os.path.basename(source_db_for_mapping)}) -> Mappé à NoteId existant: {existing[0]}",
                    flush=True)
                continue

            # Votre logique existante pour générer un nouveau GUID si le contenu diffère
            guid_to_insert = (existing and (existing[1], existing[2]) != (title, content)) and str(uuid.uuid4()) or guid

            try:
                cursor.execute("""
                    INSERT INTO Note
                      (Guid, UserMarkId, LocationId, Title, Content,
                       LastModified, Created, BlockType, BlockIdentifier)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (guid_to_insert, new_um, new_loc,
                      title, content, last_modified, created,
                      # Utilise les variables 'title' et 'content' potentiellement modifiées
                      block_type, block_identifier))
                new_id = cursor.lastrowid
                note_mapping[(source_db_for_mapping, original_note_id)] = new_id
                inserted += 1
                print(
                    f"✅ Note insérée: OldID {original_note_id} de {os.path.basename(source_db_for_mapping)} -> NewID {new_id}",
                    flush=True)
            except sqlite3.IntegrityError as ie:
                print(
                    f"❌ Erreur d'intégrité lors de l'insertion de la note {original_note_id} de {os.path.basename(source_db_for_mapping)}: {ie}",
                    flush=True)
                # Tente de récupérer l'ID si c'est un GUID qui a été inséré juste avant par une autre logique
                cursor.execute("SELECT NoteId FROM Note WHERE Guid = ?", (guid_to_insert,))
                existing_after_error = cursor.fetchone()
                if existing_after_error:
                    note_mapping[(source_db_for_mapping, original_note_id)] = existing_after_error[0]
                    print(
                        f"⏩ Récupération de l'ID existant {existing_after_error[0]} suite à un échec d'insertion (GUID {guid_to_insert})",
                        flush=True)
                else:
                    # Si l'erreur persiste et qu'aucun ID existant ne peut être trouvé, on passe cette note
                    continue

    # --- 2) AJOUTER TOUTES LES AUTRES NOTES DE db1_path non encore mappées ---
    # Cette section est maintenue telle quelle pour inclure les notes de db1 non gérées par note_choices.
    for old_note_id, guid, usermark_guid, location_id, title, content, last_mod, created, block_type, block_identifier in notes1:
        key1 = (db1_path, old_note_id)
        if key1 in note_mapping:
            continue

        # normaliser location & usermark
        norm_map = {(os.path.normpath(k[0]), k[1]): v for k, v in location_id_map.items()}
        new_loc = norm_map.get((os.path.normpath(db1_path), location_id)) if location_id else None
        new_um = usermark_guid_map.get(usermark_guid) if usermark_guid else None
        if new_loc is None:
            print(
                f"⚠️ LocationId {location_id} de note {old_note_id} de {os.path.basename(db1_path)} non mappé, ignoré (auto-inclusion).",
                flush=True)
            continue

        # existence check (votre logique de GUID existante, inchangée)
        cursor.execute("SELECT NoteId, Title, Content FROM Note WHERE Guid = ?", (guid,))
        existing = cursor.fetchone()
        if existing and existing[1] == title and existing[2] == content:
            note_mapping[key1] = existing[0]
            print(
                f"⏩ Note identique trouvée (GUID: {guid}, OldID: {old_note_id} de {os.path.basename(db1_path)}) -> Mappé à NoteId existant: {existing[0]} (auto-inclusion)",
                flush=True)
            continue

        guid_to_insert = (existing and (existing[1], existing[2]) != (title, content)) and str(uuid.uuid4()) or guid

        try:
            cursor.execute("""
                INSERT INTO Note
                (Guid, UserMarkId, LocationId, Title, Content, LastModified, Created, BlockType, BlockIdentifier)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (guid_to_insert, new_um, new_loc, title, content, last_mod, created, block_type, block_identifier))
            note_mapping[key1] = cursor.lastrowid
            inserted += 1
            print(
                f"✅ Note auto-incluse de {os.path.basename(db1_path)}: OldID {old_note_id} -> NewID {note_mapping[key1]}",
                flush=True)
        except sqlite3.IntegrityError as ie:
            print(
                f"❌ Erreur d'intégrité lors de l'auto-insertion de note {old_note_id} de {os.path.basename(db1_path)}: {ie}",
                flush=True)
            cursor.execute("SELECT NoteId FROM Note WHERE Guid = ?", (guid_to_insert,))
            existing_after_error = cursor.fetchone()
            if existing_after_error:
                note_mapping[key1] = existing_after_error[0]
                print(
                    f"⏩ Récupération de l'ID existant {existing_after_error[0]} (auto-inclusion) suite à un échec d'insertion (GUID {guid_to_insert})",
                    flush=True)
            else:
                continue

    # --- 3) AJOUTER TOUTES LES AUTRES NOTES DE db2_path non encore mappées ---
    # Cette section est maintenue telle quelle pour inclure les notes de db2 non gérées par note_choices.
    for old_note_id, guid, usermark_guid, location_id, title, content, last_mod, created, block_type, block_identifier in notes2:
        key2 = (db2_path, old_note_id)
        if key2 in note_mapping:
            continue

        norm_map = {(os.path.normpath(k[0]), k[1]): v for k, v in location_id_map.items()}
        new_loc = norm_map.get((os.path.normpath(db2_path), location_id)) if location_id else None
        new_um = usermark_guid_map.get(usermark_guid) if usermark_guid else None
        if new_loc is None:
            print(
                f"⚠️ LocationId {location_id} de note {old_note_id} de {os.path.basename(db2_path)} non mappé, ignoré (auto-inclusion).",
                flush=True)
            continue

        # existence check (votre logique de GUID existante, inchangée)
        cursor.execute("SELECT NoteId, Title, Content FROM Note WHERE Guid = ?", (guid,))
        existing = cursor.fetchone()
        if existing and existing[1] == title and existing[2] == content:
            note_mapping[key2] = existing[0]
            print(
                f"⏩ Note identique trouvée (GUID: {guid}, OldID: {old_note_id} de {os.path.basename(db2_path)}) -> Mappé à NoteId existant: {existing[0]} (auto-inclusion)",
                flush=True)
            continue

        guid_to_insert = (existing and (existing[1], existing[2]) != (title, content)) and str(uuid.uuid4()) or guid

        try:
            cursor.execute("""
                INSERT INTO Note
                (Guid, UserMarkId, LocationId, Title, Content, LastModified, Created, BlockType, BlockIdentifier)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (guid_to_insert, new_um, new_loc, title, content, last_mod, created, block_type, block_identifier))
            note_mapping[key2] = cursor.lastrowid
            inserted += 1
            print(
                f"✅ Note auto-incluse de {os.path.basename(db2_path)}: OldID {old_note_id} -> NewID {note_mapping[key2]}",
                flush=True)
        except sqlite3.IntegrityError as ie:
            print(
                f"❌ Erreur d'intégrité lors de l'auto-insertion de note {old_note_id} de {os.path.basename(db2_path)}: {ie}",
                flush=True)
            cursor.execute("SELECT NoteId FROM Note WHERE Guid = ?", (guid_to_insert,))
            existing_after_error = cursor.fetchone()
            if existing_after_error:
                note_mapping[key2] = existing_after_error[0]
                print(
                    f"⏩ Récupération de l'ID existant {existing_after_error[0]} (auto-inclusion) suite à un échec d'insertion (GUID {guid_to_insert})",
                    flush=True)
            else:
                continue

    conn.commit()
    conn.close()
    print(f"✅ Total notes insérées : {inserted}", flush=True)
    return note_mapping


def merge_usermark_with_id_relabeling(merged_db_path, source_db_path, location_id_map):
    conn_merged = sqlite3.connect(merged_db_path)
    cur_merged = conn_merged.cursor()

    # Récupère les IDs existants pour éviter les conflits
    cur_merged.execute("SELECT UserMarkId FROM UserMark")
    existing_ids = set(row[0] for row in cur_merged.fetchall())
    current_max_id = max(existing_ids) if existing_ids else 0

    # Charge les données source
    conn_source = sqlite3.connect(source_db_path)
    cur_source = conn_source.cursor()
    cur_source.execute("SELECT UserMarkId, ColorIndex, LocationId, StyleIndex, UserMarkGuid, Version FROM UserMark")
    source_rows = cur_source.fetchall()
    conn_source.close()

    # Création du mapping UserMarkId (si conflits)
    replacements = {}
    for row in source_rows:
        old_id = row[0]
        if old_id in existing_ids:
            current_max_id += 1
            new_id = current_max_id
            replacements[old_id] = new_id
        else:
            replacements[old_id] = old_id
            existing_ids.add(old_id)

    # Insertion dans la base fusionnée avec LocationId mappé
    for row in source_rows:
        old_id = row[0]
        new_id = replacements[old_id]
        ColorIndex = row[1]
        LocationId = row[2]
        StyleIndex = row[3]
        UserMarkGuid = row[4]
        Version = row[5]

        # Mapping du LocationId
        mapped_loc_id = location_id_map.get((source_db_path, LocationId), LocationId)

        try:
            cur_merged.execute("""
                INSERT INTO UserMark (UserMarkId, ColorIndex, LocationId, StyleIndex, UserMarkGuid, Version)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (new_id, ColorIndex, mapped_loc_id, StyleIndex, UserMarkGuid, Version))
        except Exception as e:
            print(f"Erreur insertion UserMark old_id={old_id}, new_id={new_id}: {e}")

    conn_merged.commit()
    conn_merged.close()
    return replacements


def merge_blockrange_from_two_sources(merged_db_path, file1_db, file2_db):
    print("\n=== FUSION BLOCKRANGE ===")

    try:
        with sqlite3.connect(merged_db_path) as dest_conn:
            dest_conn.execute("PRAGMA busy_timeout = 10000")
            dest_cursor = dest_conn.cursor()

            # 1) Vérification initiale
            dest_cursor.execute("SELECT COUNT(*) FROM BlockRange")
            print(f"BlockRanges initiaux: {dest_cursor.fetchone()[0]}")

            # 2) Récupération des mappings
            dest_cursor.execute("SELECT UserMarkId, UserMarkGuid FROM UserMark")
            usermark_guid_map = {guid: uid for uid, guid in dest_cursor.fetchall()}
            print(f"UserMark GUIDs: {usermark_guid_map}")

            # 3) Traitement des sources
            for db_path in [file1_db, file2_db]:
                print(f"\nTraitement de {db_path}")
                try:
                    with sqlite3.connect(db_path) as src_conn:
                        src_cursor = src_conn.cursor()
                        src_cursor.execute("""
                            SELECT br.BlockType, br.Identifier, br.StartToken, br.EndToken, um.UserMarkGuid
                            FROM BlockRange br
                            JOIN UserMark um ON br.UserMarkId = um.UserMarkId
                            ORDER BY br.BlockType, br.Identifier
                        """)
                        rows = src_cursor.fetchall()

                        for row in rows:
                            block_type, identifier, start_token, end_token, usermark_guid = row
                            new_usermark_id = usermark_guid_map.get(usermark_guid)

                            if not new_usermark_id:
                                print(f"⚠️ GUID non mappé: {usermark_guid}")
                                continue

                            try:
                                dest_cursor.execute("""
                                    SELECT 1 FROM BlockRange
                                    WHERE BlockType=? AND Identifier=? AND UserMarkId=?
                                    AND StartToken=? AND EndToken=?
                                """, (block_type, identifier, new_usermark_id, start_token, end_token))

                                if dest_cursor.fetchone():
                                    print(f"⏩ Existe déjà: {row}")
                                    continue

                                dest_cursor.execute("""
                                    INSERT INTO BlockRange
                                    (BlockType, Identifier, StartToken, EndToken, UserMarkId)
                                    VALUES (?, ?, ?, ?, ?)
                                """, (block_type, identifier, start_token, end_token, new_usermark_id))

                                print(f"✅ Inserté: {row}")

                            except sqlite3.IntegrityError as e:
                                print(f"❌ Erreur intégrité: {e}")
                                print(f"Ligne problématique: {row}")
                                dest_cursor.execute("PRAGMA foreign_key_check")
                                print("Problèmes clés étrangères:", dest_cursor.fetchall())
                                return False

                except Exception as e:
                    print(f"❌ Erreur lors du traitement de {db_path}: {e}")
                    return False

            # ✅ Après avoir traité les deux fichiers, on fait 1 seul commit
            try:
                dest_conn.commit()
                print(f"✅ Commit global effectué après tous les fichiers")
            except Exception as e:
                print(f"❌ Erreur critique pendant commit final : {e}")
                return False

    except Exception as e:
        print(f"❌ Erreur critique générale dans merge_blockrange_from_two_sources : {e}")
        return False

    return True


def merge_inputfields(merged_db_path, file1_db, file2_db, location_id_map):
    print("\n[🔁 PURGE + FUSION INPUTFIELD - REMAP COMPLET]")
    inserted_count = 0
    missing_count = 0

    # Lire toutes les données à réinsérer
    all_rows = []  # [(source_db, old_loc_id, tag, value)]

    for db_path in [file1_db, file2_db]:
        try:
            with sqlite3.connect(db_path) as src_conn:
                src_cursor = src_conn.cursor()
                src_cursor.execute("SELECT LocationId, TextTag, Value FROM InputField")
                for loc_id, tag, value in src_cursor.fetchall():
                    value = value if value is not None else ''
                    all_rows.append((db_path, loc_id, tag, value))
        except Exception as e:
            print(f"⚠️ Erreur lecture InputField depuis {db_path}: {e}")
            continue

    with sqlite3.connect(merged_db_path, timeout=10, check_same_thread=False) as conn:
        cursor = conn.cursor()

        # Supprimer toutes les anciennes lignes fusionnées
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS MergeMapping_InputField (
                SourceDb TEXT,
                OldLocationId INTEGER,
                TextTag TEXT,
                Value TEXT,
                PRIMARY KEY (SourceDb, OldLocationId, TextTag)
            )
        """)
        print("🧹 Suppression des anciennes données InputField fusionnées...")
        cursor.execute("""
            DELETE FROM InputField
            WHERE (LocationId, TextTag) IN (
                SELECT LocationId, TextTag
                FROM MergeMapping_InputField
            )
        """)
        cursor.execute("DELETE FROM MergeMapping_InputField")

        # Réinsertion propre
        for db_path, loc_id, tag, value in all_rows:
            mapped_loc = location_id_map.get((db_path, loc_id))
            if mapped_loc is None:
                print(f"❌ LocationId {loc_id} (depuis {db_path}) non mappé — ligne ignorée")
                missing_count += 1
                continue

            try:
                cursor.execute("""
                    INSERT INTO InputField (LocationId, TextTag, Value)
                    VALUES (?, ?, ?)
                """, (mapped_loc, tag, value))
                inserted_count += 1
                cursor.execute("""
                    INSERT INTO MergeMapping_InputField (SourceDb, OldLocationId, TextTag, Value)
                    VALUES (?, ?, ?, ?)
                """, (db_path, loc_id, tag, value))
            except sqlite3.IntegrityError as e:
                print(f"❌ Conflit à l’insertion (Loc={mapped_loc}, Tag={tag}): {e}")

        conn.commit()

    print("\n=== [INPUTFIELD FINAL] ===", flush=True)
    print(f"✅ Lignes réinsérées   : {inserted_count}", flush=True)
    print(f"❌ LocationId manquants : {missing_count}", flush=True)


def update_location_references(merged_db_path, location_replacements):
    conn = sqlite3.connect(merged_db_path)
    cursor = conn.cursor()

    for old_loc, new_loc in location_replacements.items():
        # 🔁 Mise à jour Bookmark.LocationId
        try:
            cursor.execute("UPDATE Bookmark SET LocationId = ? WHERE LocationId = ?", (new_loc, old_loc))
            print(f"Bookmark LocationId mis à jour: {old_loc} -> {new_loc}")
        except Exception as e:
            print(f"Erreur mise à jour Bookmark LocationId {old_loc}: {e}")

        # 🔁 Mise à jour Bookmark.PublicationLocationId avec conflit Slot
        try:
            cursor.execute("""
                SELECT BookmarkId, Slot FROM Bookmark
                WHERE PublicationLocationId = ?
            """, (old_loc,))
            rows = cursor.fetchall()

            for bookmark_id, slot in rows:
                cursor.execute("""
                    SELECT 1 FROM Bookmark
                    WHERE PublicationLocationId = ? AND Slot = ? AND BookmarkId != ?
                """, (new_loc, slot, bookmark_id))
                conflict = cursor.fetchone()

                if conflict:
                    print(f"⚠️ Mise à jour ignorée pour Bookmark ID {bookmark_id} (conflit avec PublicationLocationId={new_loc}, Slot={slot})")
                else:
                    cursor.execute("""
                        UPDATE Bookmark
                        SET PublicationLocationId = ?
                        WHERE BookmarkId = ?
                    """, (new_loc, bookmark_id))
                    print(f"Bookmark PublicationLocationId mis à jour: {old_loc} -> {new_loc} (BookmarkId {bookmark_id})")
        except Exception as e:
            print(f"Erreur sécurisée mise à jour PublicationLocationId {old_loc}: {e}")

        # 🔁 Mise à jour PlaylistItemLocationMap sécurisée
        try:
            cursor.execute("""
                SELECT PlaylistItemId FROM PlaylistItemLocationMap
                WHERE LocationId = ?
            """, (old_loc,))
            rows = cursor.fetchall()

            for playlist_item_id, in rows:
                cursor.execute("""
                    SELECT 1 FROM PlaylistItemLocationMap
                    WHERE PlaylistItemId = ? AND LocationId = ?
                """, (playlist_item_id, new_loc))
                conflict = cursor.fetchone()

                if conflict:
                    print(f"⚠️ Mise à jour ignorée pour PlaylistItemLocationMap: ItemId={playlist_item_id}, conflit LocationId {new_loc}")
                else:
                    cursor.execute("""
                        UPDATE PlaylistItemLocationMap
                        SET LocationId = ?
                        WHERE PlaylistItemId = ? AND LocationId = ?
                    """, (new_loc, playlist_item_id, old_loc))
                    print(f"PlaylistItemLocationMap mis à jour: ItemId={playlist_item_id}, LocationId {old_loc} -> {new_loc}")
        except Exception as e:
            print(f"Erreur mise à jour PlaylistItemLocationMap pour {old_loc} -> {new_loc}: {e}")

    conn.commit()
    try:
        conn.close()
        print("🔚 Connexion fermée dans update_location_references()")
    except Exception as e:
        print(f"❌ ERREUR lors de conn.close() : {e}")


def merge_usermark_from_sources(merged_db_path, file1_db, file2_db, location_id_map):
    print("\n[FUSION USERMARK - IDÉMPOTENTE]")
    mapping = {}

    conn = sqlite3.connect(merged_db_path)
    cursor = conn.cursor()

    # Créer la table de mapping
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS MergeMapping_UserMark (
            SourceDb TEXT,
            OldUserMarkId INTEGER,
            NewUserMarkId INTEGER,
            PRIMARY KEY (SourceDb, OldUserMarkId)
        )
    """)

    # Récupérer le dernier UserMarkId existant
    cursor.execute("SELECT COALESCE(MAX(UserMarkId), 0) FROM UserMark")
    max_id = cursor.fetchone()[0]

    for db_path in [file1_db, file2_db]:
        with sqlite3.connect(db_path) as src_conn:
            src_cursor = src_conn.cursor()
            src_cursor.execute("""
                SELECT UserMarkId, ColorIndex, LocationId, StyleIndex, UserMarkGuid, Version 
                FROM UserMark
            """)
            rows = src_cursor.fetchall()

            for old_um_id, color, loc_id, style, guid, version in rows:
                # Vérifier si déjà mappé
                cursor.execute("""
                    SELECT NewUserMarkId FROM MergeMapping_UserMark
                    WHERE SourceDb = ? AND OldUserMarkId = ?
                """, (db_path, old_um_id))
                res = cursor.fetchone()
                if res:
                    mapping[(db_path, old_um_id)] = res[0]
                    mapping[guid] = res[0]
                    continue

                # Appliquer mapping LocationId
                new_loc = location_id_map.get((db_path, loc_id), loc_id) if loc_id is not None else None

                # Vérifier si le GUID existe déjà et récupérer toutes ses données
                cursor.execute("""
                    SELECT UserMarkId, ColorIndex, LocationId, StyleIndex, Version 
                    FROM UserMark WHERE UserMarkGuid = ?
                """, (guid,))
                existing = cursor.fetchone()

                if existing:
                    existing_id, existing_color, existing_loc, existing_style, existing_version = existing

                    # Si toutes les données sont identiques, réutiliser l'ID existant
                    if (color, new_loc, style, version) == (
                    existing_color, existing_loc, existing_style, existing_version):
                        new_um_id = existing_id
                        print(f"⏩ UserMark guid={guid} déjà présent (identique), réutilisé (ID={new_um_id})")
                    else:
                        # Données différentes - générer un nouveau GUID
                        new_guid = str(uuid.uuid4())
                        max_id += 1
                        new_um_id = max_id
                        cursor.execute("""
                            INSERT INTO UserMark (UserMarkId, ColorIndex, LocationId, StyleIndex, UserMarkGuid, Version)
                            VALUES (?, ?, ?, ?, ?, ?)
                        """, (new_um_id, color, new_loc, style, new_guid, version))
                        print(
                            f"⚠️ Conflit UserMark guid={guid}, nouvelle entrée créée avec nouveau GUID (NewID={new_um_id})")
                else:
                    # Nouvel enregistrement
                    max_id += 1
                    new_um_id = max_id
                    cursor.execute("""
                        INSERT INTO UserMark (UserMarkId, ColorIndex, LocationId, StyleIndex, UserMarkGuid, Version)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (new_um_id, color, new_loc, style, guid, version))
                    print(f"✅ Insertion UserMark guid={guid}, NewID={new_um_id}")

                # Mise à jour des mappings
                mapping[(db_path, old_um_id)] = new_um_id
                mapping[guid] = new_um_id
                cursor.execute("""
                    INSERT OR REPLACE INTO MergeMapping_UserMark (SourceDb, OldUserMarkId, NewUserMarkId)
                    VALUES (?, ?, ?)
                """, (db_path, old_um_id, new_um_id))

    conn.commit()
    conn.close()
    print("Fusion UserMark terminée (idempotente).", flush=True)
    return mapping


def insert_usermark_if_needed(conn, usermark_tuple):
    """
    Insère ou met à jour un UserMark si besoin.
    usermark_tuple = (UserMarkId, ColorIndex, LocationId, StyleIndex, UserMarkGuid, Version)
    """
    (um_id, color, loc, style, guid, version) = usermark_tuple

    cur = conn.cursor()

    # Vérifie s'il existe déjà un UserMark avec ce GUID
    existing = cur.execute("""
        SELECT UserMarkId, ColorIndex, LocationId, StyleIndex, Version 
        FROM UserMark 
        WHERE UserMarkGuid = ?
    """, (guid,)).fetchone()

    if existing:
        existing_id, ex_color, ex_loc, ex_style, ex_version = existing

        if (ex_color, ex_loc, ex_style, ex_version) == (color, loc, style, version):
            # Identique -> rien à faire
            print(f"⏩ UserMark guid={guid} déjà présent et identique, insertion ignorée.")
            return

        # Sinon : on fait un UPDATE pour aligner
        print(f"⚠️ Conflit détecté pour UserMark guid={guid}. Mise à jour des champs.")
        cur.execute("""
            UPDATE UserMark
            SET ColorIndex = ?, LocationId = ?, StyleIndex = ?, Version = ?
            WHERE UserMarkGuid = ?
        """, (color, loc, style, version, guid))
    else:
        # N'existe pas → insertion
        try:
            cur.execute("""
                INSERT INTO UserMark (UserMarkId, ColorIndex, LocationId, StyleIndex, UserMarkGuid, Version)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (um_id, color, loc, style, guid, version))
            print(f"✅ UserMark guid={guid} inséré avec ID={um_id}")
        except Exception as e:
            print(f"❌ Erreur lors de l'insertion du UserMark guid={guid}: {e}")


def merge_location_from_sources(merged_db_path, file1_db, file2_db):
    """
    Fusionne les enregistrements de la table Location depuis file1 et file2
    dans la base fusionnée de façon idempotente.
    Retourne un dictionnaire {(source_db, old_id): new_id}.
    """
    # Entrée de la fonction
    print("🐞 [ENTER merge_location_from_sources]", file=sys.stderr, flush=True)
    print("\n[FUSION LOCATION - IDÉMPOTENTE]", flush=True)

    def read_locations(db_path):
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT LocationId, BookNumber, ChapterNumber, DocumentId, Track,
                       IssueTagNumber, KeySymbol, MepsLanguage, Type, Title
                FROM Location
            """)
            return [(db_path,) + row for row in cur.fetchall()]

    # Lecture combinée des deux fichiers
    locations = read_locations(file1_db) + read_locations(file2_db)

    # Connexion à la base fusionnée
    with sqlite3.connect(merged_db_path) as conn:
        cur = conn.cursor()

        # Créer la table de mapping si elle n'existe pas
        cur.execute("""
            CREATE TABLE IF NOT EXISTS MergeMapping_Location (
                SourceDb TEXT,
                OldID INTEGER,
                NewID INTEGER,
                PRIMARY KEY (SourceDb, OldID)
            )
        """)
        # Premier commit pour créer la table
        print("🐞 [BEFORE commit in merge_location_from_sources]", file=sys.stderr, flush=True)
        conn.commit()
        print("🐞 [AFTER commit in merge_location_from_sources]", file=sys.stderr, flush=True)

        # Récupérer le plus grand LocationId existant
        cur.execute("SELECT COALESCE(MAX(LocationId), 0) FROM Location")
        current_max_id = cur.fetchone()[0]

        location_id_map = {}

        for db_source, old_loc_id, book_num, chap_num, doc_id, track, issue, key_sym, meps_lang, loc_type, title in locations:
            # Vérifier mapping existant
            cur.execute("""
                SELECT NewID FROM MergeMapping_Location
                WHERE SourceDb = ? AND OldID = ?
            """, (db_source, old_loc_id))
            res = cur.fetchone()
            if res:
                new_id = res[0]
                print(f"⏩ Location déjà fusionnée OldID={old_loc_id} → NewID={new_id} (Source: {db_source})", flush=True)
                location_id_map[(db_source, old_loc_id)] = new_id
                continue

            # Recherche d'une correspondance exacte
            cur.execute("""
                SELECT LocationId FROM Location
                WHERE
                    BookNumber IS ? AND
                    ChapterNumber IS ? AND
                    DocumentId IS ? AND
                    Track IS ? AND
                    IssueTagNumber = ? AND
                    KeySymbol IS ? AND
                    MepsLanguage IS ? AND
                    Type = ? AND
                    Title IS ?
            """, (book_num, chap_num, doc_id, track, issue, key_sym, meps_lang, loc_type, title))
            existing = cur.fetchone()

            if existing:
                new_id = existing[0]
                print(f"🔎 Location existante trouvée OldID={old_loc_id} → NewID={new_id} (Source: {db_source})", flush=True)
            else:
                # Pas trouvée → insertion
                current_max_id += 1
                new_id = current_max_id
                try:
                    cur.execute("""
                        INSERT INTO Location
                        (LocationId, BookNumber, ChapterNumber, DocumentId, Track,
                         IssueTagNumber, KeySymbol, MepsLanguage, Type, Title)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (new_id, book_num, chap_num, doc_id, track, issue, key_sym, meps_lang, loc_type, title))
                    print(f"✅ Location insérée : NewID={new_id} (Source: {db_source})", flush=True)
                except sqlite3.IntegrityError as e:
                    print(f"❌ Erreur insertion Location OldID={old_loc_id}: {e}", flush=True)
                    continue

            # Mettre à jour le mapping en mémoire et dans la table de mapping
            location_id_map[(db_source, old_loc_id)] = new_id
            cur.execute("""
                INSERT OR IGNORE INTO MergeMapping_Location (SourceDb, OldID, NewID)
                VALUES (?, ?, ?)
            """, (db_source, old_loc_id, new_id))

        # Commit final pour toutes les insertions de Location
        conn.commit()

    # Sortie de la fonction
    print("🐞 [BEFORE final print in merge_location_from_sources]", file=sys.stderr, flush=True)
    print("✔ Fusion Location terminée.", file=sys.stderr, flush=True)
    print("🐞 [EXIT merge_location_from_sources]", file=sys.stderr, flush=True)

    return location_id_map


@app.route('/upload', methods=['GET', 'POST'])
def upload_files():
    if request.method == 'GET':
        response = jsonify({"message": "Route /upload fonctionne (GET) !"})
        response.headers.add("Access-Control-Allow-Origin", "*")
        return response, 200

    if 'file1' not in request.files or 'file2' not in request.files:
        return jsonify({"error": "Veuillez envoyer deux fichiers userData.db"}), 400

    file1 = request.files['file1']
    file2 = request.files['file2']

    # Définir les dossiers d'extraction (où sera placé chaque fichier userData.db)
    extracted1 = os.path.join("extracted", "file1_extracted")
    extracted2 = os.path.join("extracted", "file2_extracted")

    os.makedirs(extracted1, exist_ok=True)
    os.makedirs(extracted2, exist_ok=True)

    # Supprimer les anciens fichiers s'ils existent
    file1_path = os.path.join(extracted1, "userData.db")
    file2_path = os.path.join(extracted2, "userData.db")

    if os.path.exists(file1_path):
        os.remove(file1_path)
    if os.path.exists(file2_path):
        os.remove(file2_path)

    # Sauvegarde des fichiers userData.db
    file1.save(file1_path)
    file2.save(file2_path)

    response = jsonify({"message": "Fichiers userData.db reçus et enregistrés avec succès !"})
    response.headers.add("Access-Control-Allow-Origin", "*")
    return response, 200


@app.route('/analyze', methods=['GET'])
def validate_db_path(db_path):
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"Database not found: {db_path}")


def analyze_files():
    try:
        file1_db = os.path.join(EXTRACT_FOLDER, "file1_extracted", "userData.db")
        file2_db = os.path.join(EXTRACT_FOLDER, "file2_extracted", "userData.db")

        validate_db_path(file1_db)  # Validation ajoutée
        validate_db_path(file2_db)  # Validation ajoutée
        data1 = read_notes_and_highlights(file1_db)
        data2 = read_notes_and_highlights(file2_db)

        response = jsonify({"file1": data1, "file2": data2})
        response.headers.add("Access-Control-Allow-Origin", "*")
        return response, 200  # ← MANQUAIT ICI

    except FileNotFoundError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        app.logger.error(f"Analyze error: {str(e)}")
        return jsonify({"error": "Internal server error"}), 500


@app.route('/compare', methods=['GET'])
def compare_data():
    file1_db = os.path.join(EXTRACT_FOLDER, "file1_extracted", "userData.db")
    file2_db = os.path.join(EXTRACT_FOLDER, "file2_extracted", "userData.db")

    data1 = read_notes_and_highlights(file1_db)
    data2 = read_notes_and_highlights(file2_db)

    notes1 = {note[1]: note[2] for note in data1.get("notes", [])}
    notes2 = {note[1]: note[2] for note in data2.get("notes", [])}

    identical_notes = {}
    conflicts_notes = {}
    unique_notes_file1 = {}
    unique_notes_file2 = {}

    for title in set(notes1.keys()).intersection(set(notes2.keys())):
        if notes1[title] == notes2[title]:
            identical_notes[title] = notes1[title]
        else:
            conflicts_notes[title] = {"file1": notes1[title], "file2": notes2[title]}

    for title in set(notes1.keys()).difference(set(notes2.keys())):
        unique_notes_file1[title] = notes1[title]

    for title in set(notes2.keys()).difference(set(notes1.keys())):
        unique_notes_file2[title] = notes2[title]

    highlights1 = {h[1]: h[2] for h in data1.get("highlights", [])}
    highlights2 = {h[1]: h[2] for h in data2.get("highlights", [])}

    identical_highlights = {}
    conflicts_highlights = {}
    unique_highlights_file1 = {}
    unique_highlights_file2 = {}

    for loc in set(highlights1.keys()).intersection(set(highlights2.keys())):
        if highlights1[loc] == highlights2[loc]:
            identical_highlights[loc] = highlights1[loc]
        else:
            conflicts_highlights[loc] = {"file1": highlights1[loc], "file2": highlights2[loc]}

    for loc in set(highlights1.keys()).difference(set(highlights2.keys())):
        unique_highlights_file1[loc] = highlights1[loc]

    for loc in set(highlights2.keys()).difference(set(highights1.keys())):
        unique_highlights_file2[loc] = highlights2[loc]

    result = {
        "notes": {
            "identical": identical_notes,
            "conflicts": conflicts_notes,
            "unique_file1": unique_notes_file1,
            "unique_file2": unique_notes_file2
        },
        "highlights": {
            "identical": identical_highlights,
            "conflicts": conflicts_highlights,
            "unique_file1": unique_highlights_file1,
            "unique_file2": unique_highlights_file2
        }
    }

    response = jsonify(result)
    response.headers.add("Access-Control-Allow-Origin", "*")
    return response, 200


def merge_tags_and_tagmap(merged_db_path, file1_db, file2_db, note_mapping, location_id_map, item_id_map,
                          tag_choices_from_frontend):
    # Entrée de la fonction
    print("🐞 [ENTER merge_tags_and_tagmap]", file=sys.stderr, flush=True)
    print("\n[FUSION TAGS ET TAGMAP]", flush=True)  # Retiré "AVEC CHOIX UTILISATEUR" ici pour les tags eux-mêmes

    # 🔢 Debug : taille et clés du note_mapping
    print(f"🔢 note_mapping contient {len(note_mapping)} entrées", flush=True)
    print("🔢 Extrait des clés note_mapping :", list(note_mapping.keys())[:10], flush=True)

    with sqlite3.connect(merged_db_path, timeout=15) as conn:
        conn.execute("PRAGMA journal_mode = DELETE")
        cursor = conn.cursor()

        # Création des tables MergeMapping si nécessaire
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS MergeMapping_Tag (
                SourceDb TEXT,
                OldTagId INTEGER,
                NewTagId INTEGER,
                PRIMARY KEY (SourceDb, OldTagId)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS MergeMapping_TagMap (
                SourceDb TEXT,
                OldTagMapId INTEGER,
                NewTagMapId INTEGER,
                PRIMARY KEY (SourceDb, OldTagMapId)
            )
        """)
        conn.commit()

        # Récupération du max TagId existant
        cursor.execute("SELECT COALESCE(MAX(TagId), 0) FROM Tag")
        max_tag_id = cursor.fetchone()[0]
        tag_id_map = {}  # Mappe (source_db, old_tag_id) -> new_tag_id

        # --- Récupération des Tags sources sous forme de dictionnaires pour un accès rapide ---
        def fetch_tags_as_dict(db_path):
            tags_dict = {}
            with sqlite3.connect(db_path) as c:
                cur = c.cursor()
                cur.execute("SELECT TagId, Type, Name FROM Tag")
                for row in cur.fetchall():
                    tags_dict[row[0]] = row  # Key is TagId, value is (TagId, Type, Name)
            return tags_dict

        tags1_dict = fetch_tags_as_dict(file1_db)
        tags2_dict = fetch_tags_as_dict(file2_db)

        # --- Fusion des Tags (logique de déduplication par Type et Name - pas de choix frontend pour les Tags eux-mêmes) ---

        # 1. Traiter les tags du fichier 1
        for old_tag_id, tag_data_tuple in tags1_dict.items():
            # Vérifier si ce tag a déjà été traité (par exemple s'il était déjà dans la BD fusionnée avant ce merge)
            cursor.execute(
                "SELECT NewTagId FROM MergeMapping_Tag WHERE SourceDb=? AND OldTagId=?",
                (file1_db, old_tag_id)
            )
            res = cursor.fetchone()
            if res:
                tag_id_map[(file1_db, old_tag_id)] = res[0]
                # print(f"⏩ Tag OldID {old_tag_id} de {os.path.basename(file1_db)} déjà mappé à NewTagId {res[0]}", flush=True)
                continue

            # Déballer les données du tag
            _, tag_type, tag_name = tag_data_tuple

            # Vérifier si un tag identique (même Type et Name) existe déjà dans la DB fusionnée
            cursor.execute(
                "SELECT TagId FROM Tag WHERE Type=? AND Name=?",
                (tag_type, tag_name)
            )
            existing_tag_in_merged_db = cursor.fetchone()

            new_tag_id = None
            if existing_tag_in_merged_db:
                new_tag_id = existing_tag_in_merged_db[0]
                print(
                    f"⏩ Tag identique trouvé (F1, Name: '{tag_name}', Type: {tag_type}) -> Mappé à TagId existant: {new_tag_id}",
                    flush=True)
            else:
                max_tag_id += 1  # Incrémenter seulement pour les tags réellement nouveaux
                new_tag_id = max_tag_id
                try:
                    cursor.execute(
                        "INSERT INTO Tag (TagId, Type, Name) VALUES (?, ?, ?)",
                        (new_tag_id, tag_type, tag_name)
                    )
                    print(f"✅ Tag inséré (F1): OldID {old_tag_id} -> NewTagId {new_tag_id} (Name: '{tag_name}')",
                          flush=True)
                except sqlite3.IntegrityError as ie:
                    print(
                        f"❌ Erreur d'intégrité lors de l'insertion du tag {old_tag_id} de {os.path.basename(file1_db)}: {ie}",
                        flush=True)
                    # Tente de récupérer l'ID existant si l'erreur est due à un conflit
                    cursor.execute("SELECT TagId FROM Tag WHERE Type=? AND Name=?", (tag_type, tag_name))
                    existing_after_error = cursor.fetchone()
                    if existing_after_error:
                        new_tag_id = existing_after_error[0]
                        print(
                            f"⏩ Récupération de l'ID existant {new_tag_id} suite à un échec d'insertion (Name: '{tag_name}')",
                            flush=True)
                    else:
                        continue  # Passe ce tag si l'insertion échoue vraiment

            # Mapper l'ancien TagId source au nouveau TagId fusionné
            if new_tag_id is not None:
                tag_id_map[(file1_db, old_tag_id)] = new_tag_id
                cursor.execute(
                    "INSERT OR IGNORE INTO MergeMapping_Tag (SourceDb, OldTagId, NewTagId) VALUES (?, ?, ?)",
                    (file1_db, old_tag_id, new_tag_id)
                )

        # 2. Traiter les tags du fichier 2 (ceux qui ne sont pas déjà inclus ou dupliqués)
        for old_tag_id, tag_data_tuple in tags2_dict.items():
            # Si le tag a déjà été mappé depuis le fichier 2, passer
            cursor.execute(
                "SELECT NewTagId FROM MergeMapping_Tag WHERE SourceDb=? AND OldTagId=?",
                (file2_db, old_tag_id)
            )
            res = cursor.fetchone()
            if res:
                tag_id_map[(file2_db, old_tag_id)] = res[0]
                # print(f"⏩ Tag OldID {old_tag_id} de {os.path.basename(file2_db)} déjà mappé à NewTagId {res[0]}", flush=True)
                continue

            _, tag_type, tag_name = tag_data_tuple

            # Vérifier si un tag identique (même Type et Name) existe déjà dans la DB fusionnée
            cursor.execute(
                "SELECT TagId FROM Tag WHERE Type=? AND Name=?",
                (tag_type, tag_name)
            )
            existing_tag_in_merged_db = cursor.fetchone()

            new_tag_id = None
            if existing_tag_in_merged_db:
                new_tag_id = existing_tag_in_merged_db[0]
                print(
                    f"⏩ Tag identique trouvé (F2, Name: '{tag_name}', Type: {tag_type}) -> Mappé à TagId existant: {new_tag_id}",
                    flush=True)
            else:
                max_tag_id += 1
                new_tag_id = max_tag_id
                try:
                    cursor.execute(
                        "INSERT INTO Tag (TagId, Type, Name) VALUES (?, ?, ?)",
                        (new_tag_id, tag_type, tag_name)
                    )
                    print(f"✅ Tag inséré (F2): OldID {old_tag_id} -> NewTagId {new_tag_id} (Name: '{tag_name}')",
                          flush=True)
                except sqlite3.IntegrityError as ie:
                    print(
                        f"❌ Erreur d'intégrité lors de l'insertion du tag {old_tag_id} de {os.path.basename(file2_db)}: {ie}",
                        flush=True)
                    cursor.execute("SELECT TagId FROM Tag WHERE Type=? AND Name=?", (tag_type, tag_name))
                    existing_after_error = cursor.fetchone()
                    if existing_after_error:
                        new_tag_id = existing_after_error[0]
                        print(
                            f"⏩ Récupération de l'ID existant {new_tag_id} suite à un échec d'insertion (Name: '{tag_name}')",
                            flush=True)
                    else:
                        continue

            if new_tag_id is not None:
                tag_id_map[(file2_db, old_tag_id)] = new_tag_id
                cursor.execute(
                    "INSERT OR IGNORE INTO MergeMapping_Tag (SourceDb, OldTagId, NewTagId) VALUES (?, ?, ?)",
                    (file2_db, old_tag_id, new_tag_id)
                )

        # --- Normalisation des chemins pour note_mapping (gardé, c'est utile) ---
        normalized_note_mapping = {
            (os.path.normpath(k[0]), k[1]): v
            for k, v in note_mapping.items()
        }

        # Récupération du max TagMapId existant
        cursor.execute("SELECT COALESCE(MAX(TagMapId), 0) FROM TagMap")
        max_tagmap_id = cursor.fetchone()[0]
        tagmap_id_map = {}

        # --- Fusion des TagMap ---
        # Cette section est inchangée dans sa logique car elle n'est pas directement affectée par les choix Tom Select
        # sur les notes, mais utilise les mappings de NoteId et TagId déjà établis.
        for db_path in [file1_db, file2_db]:
            with sqlite3.connect(db_path) as src_conn:
                src_cursor = src_conn.cursor()
                src_cursor.execute("""
                    SELECT TagMapId, PlaylistItemId, LocationId, NoteId, TagId, Position
                    FROM TagMap
                """)
                rows = src_cursor.fetchall()

                # 🔢 Debug : combien de lignes TagMap lues dans cette source
                print(f"🔢 {len(rows)} TagMap rows read from {os.path.basename(db_path)}", flush=True)

                # 🔢 Debug : liste de tous les NoteId source
                all_note_ids = [r[3] for r in rows if r[3] is not None]
                distinct_ids = sorted(set(all_note_ids))
                if db_path == file1_db:
                    print(f"🔢 {len(distinct_ids)} NoteId distincts en source 1 (ex : {distinct_ids[:10]})", flush=True)

                for old_tm_id, playlist_item_id, location_id, note_id, old_tag_id, position in rows:
                    # Le new_tag_id DOIT venir de tag_id_map, qui a été rempli ci-dessus
                    new_tag_id = tag_id_map.get((db_path, old_tag_id))
                    if new_tag_id is None:
                        print(
                            f"⛔ Ignoré TagMap {old_tm_id}: Ancien TagId {old_tag_id} de {os.path.basename(db_path)} PAS trouvé dans tag_id_map. Sa TagMap ne sera pas migrée.",
                            flush=True)
                        continue

                    new_note_id = None
                    if note_id:
                        new_note_id = normalized_note_mapping.get((os.path.normpath(db_path), note_id))
                        if new_note_id is None:
                            print(
                                f"⛔ Ignoré TagMap {old_tm_id}: NoteId {note_id} de {os.path.basename(db_path)} PAS trouvée dans note_mapping. Sa TagMap ne sera pas migrée.",
                                flush=True)
                            continue

                    new_loc_id = location_id_map.get((db_path, location_id)) if location_id else None
                    new_pi_id = item_id_map.get((db_path, playlist_item_id)) if playlist_item_id else None

                    # Un TagMap doit être lié à EXACTEMENT un de ces éléments.
                    if sum(x is not None for x in [new_note_id, new_loc_id, new_pi_id]) != 1:
                        print(
                            f"⛔ Ignoré TagMap {old_tm_id}: Liaison invalide (NoteId={new_note_id}, LocId={new_loc_id}, PlaylistItemId={new_pi_id}). Doit être lié à un seul type d'élément.",
                            flush=True)
                        continue

                    # Vérification de doublon pour la TagMap (important pour éviter les insertions multiples)
                    cursor.execute("""
                        SELECT TagMapId FROM TagMap
                        WHERE TagId=?
                          AND IFNULL(PlaylistItemId,-1)=IFNULL(?, -1)
                          AND IFNULL(LocationId,-1)=IFNULL(?, -1)
                          AND IFNULL(NoteId,-1)=IFNULL(?, -1)
                          AND Position=?
                    """, (new_tag_id, new_pi_id, new_loc_id, new_note_id, position))
                    if cursor.fetchone():
                        continue  # Si une TagMap identique existe déjà, ne pas la réinsérer

                    # Votre logique d'ajustement de position (inchangée)
                    tentative = position
                    while True:
                        cursor.execute(
                            "SELECT 1 FROM TagMap WHERE TagId=? AND Position=?",
                            (new_tag_id, tentative)
                        )
                        if not cursor.fetchone():
                            break
                        tentative += 1

                    max_tagmap_id += 1
                    new_tagmap_id = max_tagmap_id
                    cursor.execute("""
                        INSERT INTO TagMap
                        (TagMapId, PlaylistItemId, LocationId, NoteId, TagId, Position)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (new_tagmap_id, new_pi_id, new_loc_id, new_note_id, new_tag_id, tentative))
                    cursor.execute("""
                        INSERT INTO MergeMapping_TagMap
                        (SourceDb, OldTagMapId, NewTagMapId)
                        VALUES (?, ?, ?)
                    """, (db_path, old_tm_id, new_tagmap_id))

                    tagmap_id_map[(db_path, old_tm_id)] = new_tagmap_id
                    print(
                        f"✅ TagMap insérée: OldTagMapId {old_tm_id} de {os.path.basename(db_path)} -> NewTagMapId {new_tagmap_id} (NewTagId: {new_tag_id}, NewNoteId: {new_note_id})",
                        flush=True)

        # 🔢 Debug : combien de TagMap ont été réellement mappées ou insérées
        print(f"🔢 Au total, {len(tagmap_id_map)} TagMap ont été mappées/inserées", flush=True)

        # Sortie de la fonction
        print("✔ Fusion des Tags et TagMap terminée.", file=sys.stderr, flush=True)
        print("🐞 [EXIT merge_tags_and_tagmap]", file=sys.stderr, flush=True)

    # Note: tag_choices_from_frontend est un paramètre mais n'est plus utilisé dans cette fonction
    # car le frontend ne passe pas de choix de fusion pour les tags eux-mêmes.
    # Il est gardé pour ne pas changer la signature de la fonction dans l'appelant.
    return tag_id_map, tagmap_id_map


def merge_playlist_items(merged_db_path, file1_db, file2_db, im_mapping=None):
    """
    Fusionne PlaylistItem de façon idempotente.
    """
    print("\n[FUSION PLAYLISTITEMS - IDÉMPOTENTE]")

    mapping = {}
    conn = sqlite3.connect(merged_db_path, timeout=30)
    conn.execute("PRAGMA busy_timeout = 10000")
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS MergeMapping_PlaylistItem (
            SourceDb TEXT,
            OldItemId INTEGER,
            NewItemId INTEGER,
            PRIMARY KEY (SourceDb, OldItemId)
        )
    """)
    conn.commit()

    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='PlaylistItem'")
    if not cursor.fetchone():
        print("[ERREUR] La table PlaylistItem n'existe pas dans la DB fusionnée.")
        conn.close()
        return {}

    import hashlib

    def safe_text(val):
        return val if val is not None else ""

    def safe_number(val):
        return val if val is not None else 0

    def generate_full_key(label, start_trim, end_trim, accuracy, end_action, thumbnail_path):
        normalized = f"{label}|{start_trim}|{end_trim}|{accuracy}|{end_action}|{thumbnail_path}"
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    existing_items = {}

    def read_playlist_items(db_path):
        with sqlite3.connect(db_path) as src_conn:
            cur_source = src_conn.cursor()
            cur_source.execute("""
                SELECT PlaylistItemId, Label, StartTrimOffsetTicks, EndTrimOffsetTicks, Accuracy, EndAction, ThumbnailFilePath
                FROM PlaylistItem
            """)
            return [(db_path,) + row for row in cur_source.fetchall()]

    all_items = read_playlist_items(file1_db) + read_playlist_items(file2_db)
    print(f"Total playlist items lus : {len(all_items)}")

    for item in all_items:
        db_source = item[0]
        old_id, label, start_trim, end_trim, accuracy, end_action, thumb_path = item[1:]

        norm_label = safe_text(label)
        norm_start = safe_number(start_trim)
        norm_end = safe_number(end_trim)
        norm_thumb = safe_text(thumb_path)

        key = generate_full_key(norm_label, norm_start, norm_end, accuracy, end_action, norm_thumb)

        cursor.execute("SELECT NewItemId FROM MergeMapping_PlaylistItem WHERE SourceDb = ? AND OldItemId = ?", (db_source, old_id))
        res = cursor.fetchone()
        if res:
            new_id = res[0]
            mapping[(db_source, old_id)] = new_id
            if key not in existing_items:
                existing_items[key] = new_id
            continue

        if key in existing_items:
            new_id = existing_items[key]
        else:
            try:
                cursor.execute("""
                    INSERT INTO PlaylistItem 
                    (Label, StartTrimOffsetTicks, EndTrimOffsetTicks, Accuracy, EndAction, ThumbnailFilePath)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (label, start_trim, end_trim, accuracy, end_action, thumb_path))
                new_id = cursor.lastrowid
                existing_items[key] = new_id
            except sqlite3.IntegrityError as e:
                print(f"Erreur insertion PlaylistItem OldID {old_id} de {db_source}: {e}")
                continue

        cursor.execute("""
            INSERT INTO MergeMapping_PlaylistItem (SourceDb, OldItemId, NewItemId)
            VALUES (?, ?, ?)
        """, (db_source, old_id, new_id))
        mapping[(db_source, old_id)] = new_id

    conn.commit()
    conn.close()
    print(f"Total PlaylistItems mappés: {len(mapping)}", flush=True)
    return mapping


def merge_playlist_item_accuracy(merged_db_path, file1_db, file2_db):
    print("\n[FUSION PLAYLISTITEMACCURACY]")

    conn = sqlite3.connect(merged_db_path)
    cursor = conn.cursor()

    cursor.execute("SELECT COALESCE(MAX(PlaylistItemAccuracyId), 0) FROM PlaylistItemAccuracy")
    max_acc_id = cursor.fetchone()[0] or 0
    print(f"ID max initial: {max_acc_id}")

    for db_path in [file1_db, file2_db]:
        try:
            with sqlite3.connect(db_path) as src_conn:
                src_cursor = src_conn.cursor()
                src_cursor.execute("SELECT PlaylistItemAccuracyId, Description FROM PlaylistItemAccuracy")
                records = src_cursor.fetchall()
                print(f"{len(records)} records trouvés dans {os.path.basename(db_path)}")

                for acc_id, desc in records:
                    cursor.execute("""
                        INSERT OR IGNORE INTO PlaylistItemAccuracy (PlaylistItemAccuracyId, Description)
                        VALUES (?, ?)
                    """, (acc_id, desc))
                    max_acc_id = max(max_acc_id, acc_id)
        except Exception as e:
            print(f"⚠️ Erreur lors du traitement de {db_path}: {e}")

    conn.commit()
    conn.close()
    print(f"ID max final: {max_acc_id}", flush=True)
    return max_acc_id


def merge_playlist_item_location_map(merged_db_path, file1_db, file2_db, item_id_map, location_id_map):
    print("\n[FUSION PLAYLISTITEMLOCATIONMAP]")

    conn = sqlite3.connect(merged_db_path)
    cursor = conn.cursor()

    # Étape 1: Vider complètement la table
    cursor.execute("DELETE FROM PlaylistItemLocationMap")
    print("🗑️ Table PlaylistItemLocationMap vidée avant reconstruction")

    total_inserted = 0
    total_skipped = 0

    # Étape 2: Reconstruction avec mapping
    for db_path in [file1_db, file2_db]:
        normalized_db = os.path.normpath(db_path)
        with sqlite3.connect(db_path) as src_conn:
            src_cursor = src_conn.cursor()
            src_cursor.execute("""
                SELECT PlaylistItemId, LocationId, MajorMultimediaType, BaseDurationTicks
                FROM PlaylistItemLocationMap
            """)
            mappings = src_cursor.fetchall()
            print(f"{len(mappings)} mappings trouvés dans {os.path.basename(db_path)}")

            for old_item_id, old_loc_id, mm_type, duration in mappings:
                new_item_id = item_id_map.get((normalized_db, old_item_id))
                new_loc_id = location_id_map.get((normalized_db, old_loc_id))

                if new_item_id is None or new_loc_id is None:
                    print(f"⚠️ Ignoré: PlaylistItemId={old_item_id} ou LocationId={old_loc_id} non mappé (source: {os.path.basename(db_path)})")
                    total_skipped += 1
                    continue

                try:
                    cursor.execute("""
                        INSERT INTO PlaylistItemLocationMap
                        (PlaylistItemId, LocationId, MajorMultimediaType, BaseDurationTicks)
                        VALUES (?, ?, ?, ?)
                    """, (new_item_id, new_loc_id, mm_type, duration))
                    print(f"✅ Insertion: PlaylistItemId={new_item_id}, LocationId={new_loc_id}")
                    total_inserted += 1
                except sqlite3.IntegrityError as e:
                    print(f"⚠️ Doublon ignoré: {e}")
                    total_skipped += 1

    conn.commit()

    print(f"📊 Résultat: {total_inserted} lignes insérées, {total_skipped} ignorées")
    cursor.execute("SELECT COUNT(*) FROM PlaylistItemLocationMap")
    count = cursor.fetchone()[0]
    print(f"🔍 Total final dans PlaylistItemLocationMap: {count} lignes", flush=True)

    conn.close()


def cleanup_playlist_item_location_map(conn):
    cursor = conn.cursor()
    cursor.execute("""
        DELETE FROM PlaylistItemLocationMap
        WHERE PlaylistItemId NOT IN (
            SELECT PlaylistItemId FROM PlaylistItem
        )
    """)
    conn.commit()
    print("🧹 Nettoyage post-merge : PlaylistItemLocationMap nettoyée.", flush=True)


def merge_playlist_item_independent_media_map(merged_db_path, file1_db, file2_db, item_id_map, independent_media_map):
    """
    Fusionne PlaylistItemIndependentMediaMap avec adaptation du mapping.
    """
    print("\n[FUSION PlaylistItemIndependentMediaMap]")
    conn = sqlite3.connect(merged_db_path)
    cursor = conn.cursor()

    # 🧹 On vide la table avant de la reconstruire proprement
    cursor.execute("DELETE FROM PlaylistItemIndependentMediaMap")

    inserted = 0
    skipped = 0

    for db_path in [file1_db, file2_db]:
        normalized_db = os.path.normpath(db_path)
        with sqlite3.connect(db_path) as src_conn:
            src_cursor = src_conn.cursor()
            src_cursor.execute("""
                SELECT PlaylistItemId, IndependentMediaId, DurationTicks
                FROM PlaylistItemIndependentMediaMap
            """)
            rows = src_cursor.fetchall()
            print(f"{len(rows)} lignes trouvées dans {os.path.basename(db_path)}")

            for old_item_id, old_media_id, duration_ticks in rows:
                new_item_id = item_id_map.get((normalized_db, old_item_id))
                new_media_id = independent_media_map.get((normalized_db, old_media_id))

                if new_item_id is None or new_media_id is None:
                    print(f"⚠️ Mapping manquant pour PlaylistItemId={old_item_id}, IndependentMediaId={old_media_id} (source: {normalized_db})")
                    skipped += 1
                    continue

                try:
                    cursor.execute("""
                        INSERT INTO PlaylistItemIndependentMediaMap
                        (PlaylistItemId, IndependentMediaId, DurationTicks)
                        VALUES (?, ?, ?)
                    """, (new_item_id, new_media_id, duration_ticks))
                    inserted += 1
                except sqlite3.IntegrityError as e:
                    print(f"🚫 Doublon ignoré : {e}")
                    skipped += 1

    conn.commit()
    conn.close()
    print(f"✅ PlaylistItemIndependentMediaMap : {inserted} insérés, {skipped} ignorés.", flush=True)


def merge_playlist_item_marker(merged_db_path, file1_db, file2_db, item_id_map):
    """
    Fusionne la table PlaylistItemMarker de façon idempotente.
    Retourne marker_id_map.
    """
    print("\n[FUSION PLAYLISTITEMMARKER]")
    conn = sqlite3.connect(merged_db_path)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS MergeMapping_PlaylistItemMarker (
            SourceDb TEXT,
            OldMarkerId INTEGER,
            NewMarkerId INTEGER,
            PRIMARY KEY (SourceDb, OldMarkerId)
        )
    """)
    conn.commit()

    cursor.execute("SELECT COALESCE(MAX(PlaylistItemMarkerId), 0) FROM PlaylistItemMarker")
    max_marker_id = cursor.fetchone()[0] or 0
    print(f"ID max initial: {max_marker_id}")
    marker_id_map = {}

    for db_path in [file1_db, file2_db]:
        normalized_db = os.path.normpath(db_path)
        with sqlite3.connect(db_path) as src_conn:
            src_cursor = src_conn.cursor()
            src_cursor.execute("""
                SELECT PlaylistItemMarkerId, PlaylistItemId, Label, StartTimeTicks, DurationTicks, EndTransitionDurationTicks
                FROM PlaylistItemMarker
            """)
            markers = src_cursor.fetchall()
            print(f"{len(markers)} markers trouvés dans {os.path.basename(db_path)}")

            for old_marker_id, old_item_id, label, start_time, duration, end_transition in markers:
                new_item_id = item_id_map.get((normalized_db, old_item_id))
                if not new_item_id:
                    print(f"    > ID item introuvable pour marker {old_marker_id} — ignoré")
                    continue

                # Utiliser la version normalisée aussi ici
                cursor.execute("""
                    SELECT NewMarkerId FROM MergeMapping_PlaylistItemMarker
                    WHERE SourceDb = ? AND OldMarkerId = ?
                """, (normalized_db, old_marker_id))
                res = cursor.fetchone()
                if res:
                    marker_id_map[(normalized_db, old_marker_id)] = res[0]
                    continue

                max_marker_id += 1
                new_row = (max_marker_id, new_item_id, label, start_time, duration, end_transition)

                try:
                    cursor.execute("""
                        INSERT INTO PlaylistItemMarker
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, new_row)
                    marker_id_map[(normalized_db, old_marker_id)] = max_marker_id
                    cursor.execute("""
                        INSERT INTO MergeMapping_PlaylistItemMarker (SourceDb, OldMarkerId, NewMarkerId)
                        VALUES (?, ?, ?)
                    """, (normalized_db, old_marker_id, max_marker_id))
                    conn.commit()
                except sqlite3.IntegrityError as e:
                    print(f"🚫 Erreur insertion PlaylistItemMarker pour OldMarkerId {old_marker_id}: {e}")

    print(f"ID max final: {max_marker_id}")
    print(f"Total markers mappés: {len(marker_id_map)}")
    conn.close()
    return marker_id_map


def merge_marker_maps(merged_db_path, file1_db, file2_db, marker_id_map):
    """
    Fusionne les tables de mapping liées aux markers, y compris PlaylistItemMarkerBibleVerseMap
    et PlaylistItemMarkerParagraphMap.
    """
    print("\n[FUSION MARKER MAPS]")
    conn = sqlite3.connect(merged_db_path)
    cursor = conn.cursor()

    for map_type in ['BibleVerse', 'Paragraph']:
        table_name = f'PlaylistItemMarker{map_type}Map'
        cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table_name}'")
        if not cursor.fetchone():
            print(f"Table {table_name} non trouvée - ignorée")
            continue
        print(f"\nFusion de {table_name}")

        for db_path in [file1_db, file2_db]:
            normalized_db = os.path.normpath(db_path)
            with sqlite3.connect(db_path) as src_conn:
                src_cursor = src_conn.cursor()
                src_cursor.execute(f"SELECT * FROM {table_name}")
                rows = src_cursor.fetchall()
                print(f"{len(rows)} entrées trouvées dans {os.path.basename(db_path)} pour {table_name}")

                for row in rows:
                    old_marker_id = row[0]
                    new_marker_id = marker_id_map.get((normalized_db, old_marker_id))
                    if not new_marker_id:
                        continue
                    new_row = (new_marker_id,) + row[1:]
                    placeholders = ",".join(["?"] * len(new_row))
                    try:
                        cursor.execute(f"INSERT OR IGNORE INTO {table_name} VALUES ({placeholders})", new_row)
                    except sqlite3.IntegrityError as e:
                        print(f"Erreur dans {table_name}: {e}")

    conn.commit()
    conn.close()


def merge_playlists(merged_db_path, file1_db, file2_db, location_id_map, independent_media_map, item_id_map):
    """Fusionne toutes les tables liées aux playlists en respectant les contraintes."""
    print("\n=== DÉBUT FUSION PLAYLISTS ===")

    file1_db = os.path.normpath(file1_db)
    file2_db = os.path.normpath(file2_db)

    max_media_id = 0  # 🔧 Ajout essentiel
    max_playlist_id = 0  # 🔧 pour éviter 'not associated with a value'
    conn = None  # 🧷 Pour pouvoir le fermer plus tard

    try:
        conn = sqlite3.connect(merged_db_path, timeout=30)
        conn.execute("PRAGMA busy_timeout = 10000")
        cursor = conn.cursor()

        print("\n[INITIALISATION]")
        print(f"Base de fusion: {merged_db_path}")
        print(f"Source 1: {file1_db}")
        print(f"Source 2: {file2_db}")
        print(f"Location IDs mappés: {len(location_id_map)}")

        item_id_map = merge_playlist_items(
            merged_db_path, file1_db, file2_db, independent_media_map  # ✅ 4 max
        )

        # Appel immédiat à merge_playlist_items pour avoir item_id_map dispo dès le début
        print(f"Mapping PlaylistItems: {item_id_map}")

        # ... (la suite continue normalement)

        marker_id_map = {}

        # 1. Fusion de PlaylistItemAccuracy
        max_acc_id = merge_playlist_item_accuracy(merged_db_path, file1_db, file2_db)
        print(f"--> PlaylistItemAccuracy fusionnée, max ID final: {max_acc_id}")

        # 2. Fusion PlaylistItemMarker
        # Fusion de PlaylistItemMarker et récupération du mapping des markers
        marker_id_map = merge_playlist_item_marker(merged_db_path, file1_db, file2_db, item_id_map)
        print(f"--> PlaylistItemMarker fusionnée, markers mappés: {len(marker_id_map)}")

        # 3. Fusion des PlaylistItemMarkerMap et Marker*Map (BibleVerse/Paragraph)
        print("\n[FUSION MARKER MAPS]")

        # 4. Fusion des PlaylistItemMarkerBibleVerseMap et ParagraphMap
        # Fusion des MarkerMaps (BibleVerse, Paragraph, etc.)
        merge_marker_maps(merged_db_path, file1_db, file2_db, marker_id_map)
        print("--> MarkerMaps fusionnées.")

        # 5. Fusion de PlaylistItemIndependentMediaMap
        # Fusion de PlaylistItemIndependentMediaMap (basée sur PlaylistItemIndependentMediaMap)
        merge_playlist_item_independent_media_map(merged_db_path, file1_db, file2_db, item_id_map, independent_media_map)
        print("--> PlaylistItemIndependentMediaMap fusionnée.")

        # 6. Fusion PlaylistItemLocationMap
        merge_playlist_item_location_map(merged_db_path, file1_db, file2_db, item_id_map, location_id_map)
        print("--> PlaylistItemLocationMap fusionnée.")

        # Nettoyage : retirer les mappings avec des PlaylistItemId fantômes
        with sqlite3.connect(merged_db_path) as conn:
            cleanup_playlist_item_location_map(conn)

        # ========================
        # Maintenant, on démarre les opérations qui ouvrent leurs propres connexions
        # ========================

        # 7. Fusion de la table IndependentMedia (améliorée)
        print("\n[FUSION INDEPENDENTMEDIA]")
        # On réutilise le mapping déjà préparé dans merge_data
        im_mapping = independent_media_map

        # 8. Vérification finale des thumbnails
        print("\n[VÉRIFICATION THUMBNAILS ORPHELINS]")
        cursor.execute("""
            SELECT p.PlaylistItemId, p.ThumbnailFilePath
            FROM PlaylistItem p
            WHERE p.ThumbnailFilePath IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM IndependentMedia m 
                  WHERE m.FilePath = p.ThumbnailFilePath
              )
        """)
        orphaned_thumbnails = cursor.fetchall()
        if orphaned_thumbnails:
            print(f"Avertissement : {len(orphaned_thumbnails)} thumbnails sans média associé")

            # ✅ Ajoute ceci ici (pas en dehors)
            conn.commit()

        # 9. Finalisation playlists
        print("\n=== FUSION PLAYLISTS TERMINÉE ===")
        playlist_results = {
            'item_id_map': item_id_map,
            'marker_id_map': marker_id_map,
            'media_status': {
                'total_media': max_media_id,
                'orphaned_thumbnails': len(orphaned_thumbnails) if 'orphaned_thumbnails' in locals() else 0
            }
        }
        print(f"Résumé intermédiaire: {playlist_results}")

        # 10. Finalisation
        # commit final et fermeture propre
        conn.commit()

        # 🔚 Fin de merge_playlists (retour principal)
        orphaned_deleted = 0  # ou remplace par la vraie valeur si elle est calculée plus haut
        playlist_item_total = len(item_id_map)

        print("\n🧪 DEBUG FINAL DANS merge_playlists")
        print("Item ID Map complet:", flush=True)
        for (src, old_id), new_id in item_id_map.items():
            print(f"  {src} — {old_id} → {new_id}")

        with sqlite3.connect(merged_db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA quick_check")
            integrity_result = cursor.fetchone()[0]

        return (
            max_playlist_id,
            len(item_id_map),
            max_media_id,
            orphaned_deleted,
            integrity_result,
            item_id_map
        )

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"ERREUR CRITIQUE dans merge_playlists: {str(e)}")
        return None, 0, 0, 0, "error", {}

    finally:
        if conn:
            try:
                conn.close()
            except:
                pass


def check_duplicate_guids_between_sources(file1_db, file2_db):
    """Vérifie s'il y a des GUIDs en commun entre les deux sources"""
    guids_file1 = set()
    guids_file2 = set()

    with sqlite3.connect(file1_db) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT UserMarkGuid FROM UserMark")
        guids_file1 = {row[0] for row in cursor.fetchall()}

    with sqlite3.connect(file2_db) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT UserMarkGuid FROM UserMark")
        guids_file2 = {row[0] for row in cursor.fetchall()}

    return guids_file1 & guids_file2


def create_note_mapping(merged_db_path, file1_db, file2_db):
    """Crée un mapping (source_db_path, old_note_id) -> new_note_id en se basant sur les GUID."""
    mapping = {}
    try:
        with sqlite3.connect(merged_db_path, timeout=30) as merged_conn:
            merged_conn.execute("PRAGMA busy_timeout = 10000")
            merged_cursor = merged_conn.cursor()
            merged_cursor.execute("SELECT Guid, NoteId FROM Note")
            merged_guid_map = {guid: note_id for guid, note_id in merged_cursor.fetchall() if guid}

        for db_path in [file1_db, file2_db]:
            if not os.path.exists(db_path):
                print(f"[WARN] Fichier DB manquant : {db_path}")
                continue

            with sqlite3.connect(db_path) as src_conn:
                src_cursor = src_conn.cursor()
                src_cursor.execute("SELECT NoteId, Guid FROM Note")
                for old_note_id, guid in src_cursor.fetchall():
                    if guid and guid in merged_guid_map:
                        mapping[(db_path, old_note_id)] = merged_guid_map[guid]

    except Exception as e:
        print(f"[ERREUR] create_note_mapping: {str(e)}")

    return mapping or {}


def merge_platform_metadata(merged_db_path, db1_path, db2_path):
    print("🔧 Fusion combinée android_metadata + grdb_migrations")
    locales = set()
    identifiers = set()

    for db_path in [db1_path, db2_path]:
        with sqlite3.connect(db_path) as src:
            cursor = src.cursor()
            try:
                cursor.execute("SELECT locale FROM android_metadata")
                locales.update(row[0] for row in cursor.fetchall())
            except sqlite3.OperationalError:
                print(f"ℹ️ Table android_metadata absente de {db_path}")
            except Exception as e:
                print(f"⚠️ Erreur lecture android_metadata depuis {db_path}: {e}")

            try:
                cursor.execute("SELECT identifier FROM grdb_migrations")
                identifiers.update(row[0] for row in cursor.fetchall())
            except sqlite3.OperationalError:
                print(f"ℹ️ Table grdb_migrations absente de {db_path}")
            except Exception as e:
                print(f"⚠️ Erreur lecture grdb_migrations depuis {db_path}: {e}")

    # Une seule connexion d’écriture pour les deux insertions
    with sqlite3.connect(merged_db_path, timeout=15) as conn:
        cursor = conn.cursor()

        if locales:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS android_metadata (
                    locale TEXT
                )
            """)
            cursor.execute("DELETE FROM android_metadata")
            for loc in locales:
                print(f"✅ INSERT android_metadata.locale = {loc}")
                cursor.execute("INSERT INTO android_metadata (locale) VALUES (?)", (loc,))

        if identifiers:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS grdb_migrations (
                    identifier TEXT NOT NULL PRIMARY KEY
                )
            """)
            cursor.execute("DELETE FROM grdb_migrations")
            for ident in sorted(identifiers):
                print(f"✅ INSERT grdb_migrations.identifier = {ident}")
                cursor.execute("INSERT INTO grdb_migrations (identifier) VALUES (?)", (ident,))


def apply_selected_tags(merged_db_path, db1_path, db2_path, note_choices, note_mapping, tag_id_map):
    print("\n[🔁 APPLICATION DES selectedTags — VERSION FIABLE AVEC ID RÉELS]", flush=True)
    #print("Données reçues par le backend (extrait de note_choices) :", {k: {kk: vv for kk, vv in v.items() if kk != 'edited'} for k, v in note_choices.items() if isinstance(v, dict)}, flush=True) # Pour ne pas spammer avec 'edited'

    with sqlite3.connect(merged_db_path) as conn:
        cursor = conn.cursor()

        for index_str, note_data in note_choices.items():
            if not isinstance(note_data, dict):
                print(f"⚠️ Données de note inattendues pour index {index_str}: {note_data}", flush=True)
                continue

            choice = note_data.get("choice")
            if choice == "ignore":
                print(f"⏩ Note {index_str} ignorée, pas d'application de tags.", flush=True)
                continue

            note_ids = note_data.get("noteIds", {})
            if not isinstance(note_ids, dict):
                print(f"⚠️ 'noteIds' inattendues pour note {index_str}: {note_ids}", flush=True)
                continue

            # === Cas "both" → un seul tableau selectedTags à appliquer aux deux notes ===
            if choice == "both":
                selected_tags = note_data.get("selectedTags", []) # Liste des anciens TagId choisis
                if not isinstance(selected_tags, list):
                    print(f"⚠️ 'selectedTags' inattendues pour note {index_str} (choice='both'): {selected_tags}", flush=True)
                    continue

                # Appliquer les tags aux deux notes (si elles existent) qui ont été fusionnées
                for source in ["file1", "file2"]:
                    old_note_id = note_ids.get(source)
                    if not old_note_id:
                        continue # La note n'existait peut-être pas dans cette source

                    # Déterminer le chemin de la DB source (nécessaire pour note_mapping et tag_id_map)
                    source_db = db1_path if source == "file1" else db2_path

                    # Récupérer le nouvel NoteId dans la base fusionnée
                    # Note: note_mapping utilise le chemin normalisé (os.path.normpath)
                    normalized_source_db = os.path.normpath(source_db)
                    new_note_id = note_mapping.get((normalized_source_db, old_note_id))

                    if not new_note_id:
                        print(f"⚠️ Note fusionnée introuvable pour {source_db} OldID {old_note_id}. Tags non appliqués.", flush=True)
                        continue

                    print(f"🔄 Application des tags pour NoteID fusionné: {new_note_id} (orig. {source_db} OldID {old_note_id})", flush=True)

                    # Supprimer toutes les TagMap existantes pour cette note dans la DB fusionnée
                    cursor.execute("DELETE FROM TagMap WHERE NoteId = ?", (new_note_id,))
                    print(f"🗑️ Supprimé les TagMap existantes pour NoteId {new_note_id}.", flush=True)

                    # Insérer les nouveaux TagMap basés sur les tags sélectionnés
                    for tag_id_from_frontend in selected_tags: # tag_id_from_frontend est l'ancien TagId de la source
                        # Trouver le nouveau TagId correspondant dans la DB fusionnée
                        # tag_id_map a été construit par merge_tags_and_tagmap: (source_db, old_tag_id) -> new_tag_id
                        new_tag_id = tag_id_map.get((normalized_source_db, tag_id_from_frontend))
                        if new_tag_id is None:
                            print(f"⚠️ Ancien TagId {tag_id_from_frontend} de {source_db} non trouvé dans tag_id_map. Ce tag ne sera pas lié.", flush=True)
                            continue

                        # Trouver la prochaine position disponible pour ce TagId (standard JW Library)
                        cursor.execute("""
                            SELECT COALESCE(MAX(Position), 0) + 1 FROM TagMap WHERE TagId = ?
                        """, (new_tag_id,))
                        position = cursor.fetchone()[0]

                        print(f"📝 Insertion TagMap: NoteId={new_note_id}, TagId_new={new_tag_id} (orig. {tag_id_from_frontend}), Position={position}", flush=True)

                        cursor.execute("""
                            INSERT INTO TagMap (NoteId, TagId, Position)
                            VALUES (?, ?, ?)
                        """, (new_note_id, new_tag_id, position))

            # === Cas "file1" ou "file2" → on applique uniquement au fichier choisi ===
            elif choice in ("file1", "file2"):
                old_note_id = note_ids.get(choice)
                if not old_note_id:
                    continue # La note n'existait pas dans la source choisie

                # Récupérer les tags sélectionnés spécifiquement pour la source choisie
                selected_tags = note_data.get("selectedTagsPerSource", {}).get(choice, [])
                if not isinstance(selected_tags, list):
                    print(f"⚠️ 'selectedTagsPerSource' inattendues pour note {index_str} (choice='{choice}'): {selected_tags}", flush=True)
                    continue

                source_db = db1_path if choice == "file1" else db2_path
                normalized_source_db = os.path.normpath(source_db)
                new_note_id = note_mapping.get((normalized_source_db, old_note_id))
                if not new_note_id:
                    print(f"⚠️ Note fusionnée introuvable pour {source_db} OldID {old_note_id}. Tags non appliqués.", flush=True)
                    continue

                print(f"🔄 Application des tags pour NoteID fusionné: {new_note_id} (orig. {source_db} OldID {old_note_id})", flush=True)

                # Supprimer toutes les TagMap existantes pour cette note dans la DB fusionnée
                cursor.execute("DELETE FROM TagMap WHERE NoteId = ?", (new_note_id,))
                print(f"🗑️ Supprimé les TagMap existantes pour NoteId {new_note_id}.", flush=True)

                # Insérer les nouveaux TagMap basés sur les tags sélectionnés
                for tag_id_from_frontend in selected_tags: # tag_id_from_frontend est l'ancien TagId de la source
                    # Trouver le nouveau TagId correspondant dans la DB fusionnée
                    new_tag_id = tag_id_map.get((normalized_source_db, tag_id_from_frontend))
                    if new_tag_id is None:
                        print(f"⚠️ Ancien TagId {tag_id_from_frontend} de {source_db} non trouvé dans tag_id_map. Ce tag ne sera pas lié.", flush=True)
                        continue

                    # Trouver la prochaine position disponible pour ce TagId
                    cursor.execute("""
                        SELECT COALESCE(MAX(Position), 0) + 1 FROM TagMap WHERE TagId = ?
                    """, (new_tag_id,))
                    position = cursor.fetchone()[0]

                    print(f"📝 Insertion TagMap: NoteId={new_note_id}, TagId_new={new_tag_id} (orig. {tag_id_from_frontend}), Position={position}", flush=True)

                    cursor.execute("""
                        INSERT INTO TagMap (NoteId, TagId, Position)
                        VALUES (?, ?, ?)
                    """, (new_note_id, new_tag_id, position))

        conn.commit()
    print("✅ Tags appliqués correctement avec les vrais NoteId.", flush=True)


@app.route('/merge', methods=['POST'])
def merge_data():
    start_time = time.time()
    # Au tout début du merge
    open(os.path.join(UPLOAD_FOLDER, "merge_in_progress"), "w").close()
    print("🐞 [ENTER merge_data]", flush=True)

    # ─── 0. Initialisation des variables utilisées plus bas ─────────────────────────────
    merged_jwlibrary = None
    max_playlist_id = 0
    max_media_id = 0
    orphaned_deleted = 0
    integrity_result = "ok"
    item_id_map = {}
    marker_id_map = {}
    playlist_id_map = {}
    tag_id_map = {}
    tagmap_id_map = {}
    note_mapping = {}

    conn = None  # pour le finally

    try:
        payload = request.get_json()
        print("🔍 Payload JSON reçu par le backend:", json.dumps(payload, indent=2), flush=True)
        choix_client = payload.get("choices", {})
        choix_notes_client = choix_client.get("notes", {})
        choix_marque_pages_client = choix_client.get("bookmarks", {})
        local_datetime = payload.get("local_datetime")
        print(f"local_datetime reçu du client : {local_datetime}")
        if local_datetime:
            merge_date = local_datetime if len(local_datetime) > 16 else local_datetime + ":00"
        else:
            merge_date = get_current_local_iso8601()

        file1_db = os.path.join(EXTRACT_FOLDER, "file1_extracted", "userData.db")
        file2_db = os.path.join(EXTRACT_FOLDER, "file2_extracted", "userData.db")

        # Validation fichiers sources
        if not all(os.path.exists(db) for db in [file1_db, file2_db]):
            return jsonify({"error": "Fichiers source manquants"}), 400

        data1 = read_notes_and_highlights(file1_db)
        data2 = read_notes_and_highlights(file2_db)

        highlights_db1 = data1["highlights"]
        highlights_db2 = data2["highlights"]
        merged_highlights_dict = {}
        for h in highlights_db1:
            _, color, loc, style, guid, version = h
            merged_highlights_dict[guid] = (color, loc, style, version)
        for h in highlights_db2:
            _, color2, loc2, style2, guid2, version2 = h
            if guid2 not in merged_highlights_dict:
                merged_highlights_dict[guid2] = (color2, loc2, style2, version2)
            else:
                (color1, loc1, style1, version1) = merged_highlights_dict[guid2]
                if (color1 == color2 and loc1 == loc2 and style1 == style2 and version1 == version2):
                    continue
                else:
                    choice = conflict_choices_highlights.get(guid2, "file1")
                    if choice == "file2":
                        merged_highlights_dict[guid2] = (color2, loc2, style2, version2)

        # === Validation préalable ===
        required_dbs = [
            os.path.join(EXTRACT_FOLDER, "file1_extracted", "userData.db"),
            os.path.join(EXTRACT_FOLDER, "file2_extracted", "userData.db")
        ]
        if not all(os.path.exists(db) for db in required_dbs):
            return jsonify({"error": "Fichiers source manquants"}), 400

        # Création de la DB fusionnée
        merged_db_path = os.path.join(UPLOAD_FOLDER, "merged_userData.db")
        if os.path.exists(merged_db_path):
            os.remove(merged_db_path)
        base_db_path = os.path.join(EXTRACT_FOLDER, "file1_extracted", "userData.db")
        create_merged_schema(merged_db_path, base_db_path)

        # juste après create_merged_schema(merged_db_path, base_db_path)
        print("\n→ Debug: listing des tables juste après create_merged_schema")
        with sqlite3.connect(merged_db_path) as dbg_conn:
            dbg_cur = dbg_conn.cursor()
            dbg_cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [t[0] for t in dbg_cur.fetchall()]
            print("Tables présentes dans merged_userData.db :", tables)

        # ── Fusion des Location ──
        print("🐞 [BEFORE merge_location_from_sources]", flush=True)
        try:
            location_id_map = merge_location_from_sources(merged_db_path, *required_dbs)
            # Si on arrive ici, la fonction s’est bien terminée
            print("🐞 [AFTER merge_location_from_sources]", flush=True)
            print("Location ID Map:", location_id_map, flush=True)
        except Exception as e:
            import traceback
            print("❌ Exception DANS merge_location_from_sources :", e, flush=True)
            traceback.print_exc()
            raise

        try:
            independent_media_map = merge_independent_media(merged_db_path, file1_db, file2_db)
            print("Mapping IndependentMedia:", independent_media_map)
        except Exception as e:
            import traceback
            print(f"❌ Erreur dans merge_independent_media : {e}")
            traceback.print_exc()
            raise

        # ❌ NE PAS appeler merge_playlist_items ici
        # item_id_map = merge_playlist_items(...)

        common_guids = check_duplicate_guids_between_sources(file1_db, file2_db)
        if common_guids:
            print(f"⚠️ Attention: {len(common_guids)} GUIDs en commun entre les deux sources")
            # Afficher les 5 premiers pour le debug
            for guid in list(common_guids)[:5]:
                print(f"- {guid}")
        else:
            print("✅ Aucun GUID en commun entre les deux sources")

        print("🐞 [BEFORE merge_usermark_from_sources]", flush=True)

        try:
            usermark_guid_map = merge_usermark_from_sources(merged_db_path, file1_db, file2_db, location_id_map)

        except Exception as e:
            import traceback
            print(f"❌ Erreur dans merge_usermark_from_sources : {e}")
            traceback.print_exc()
            raise

        print("🐞 [AFTER merge_usermark_from_sources]", flush=True)

        # Après le bloc try/except de merge_usermark_from_sources
        with sqlite3.connect(merged_db_path) as conn:
            cursor = conn.cursor()
            # Vérifier les doublons potentiels
            cursor.execute("""
                SELECT UserMarkGuid, COUNT(*) as cnt 
                FROM UserMark 
                GROUP BY UserMarkGuid 
                HAVING cnt > 1
            """)
            duplicates = cursor.fetchall()
            if duplicates:
                print("⚠️ Attention: GUIDs dupliqués détectés après fusion:")
                for guid, count in duplicates:
                    print(f"- {guid}: {count} occurrences")
            else:
                print("✅ Aucun GUID dupliqué détecté après fusion")

        # Gestion spécifique de LastModified
        conn = sqlite3.connect(merged_db_path)
        cursor = conn.cursor()

        cursor.execute("DELETE FROM LastModified")
        cursor.execute("INSERT INTO LastModified (LastModified) VALUES (?)", (merge_date,))
        conn.commit()
        conn.close()

        try:
            note_mapping = create_note_mapping(merged_db_path, file1_db, file2_db)
            print("Note Mapping:", note_mapping)
        except Exception as e:
            import traceback
            print(f"❌ Erreur dans create_note_mapping : {e}")
            traceback.print_exc()
            raise

        # (Ré)ouvrir la connexion pour PlaylistItem
        conn = sqlite3.connect(merged_db_path)
        cursor = conn.cursor()

        print(f"--> PlaylistItem fusionnés : {len(item_id_map)} items")

        conn.close()

        print("\n=== USERMARK VERIFICATION ===")
        print(f"Total UserMarks mappés (GUIDs) : {len(usermark_guid_map)}")
        with sqlite3.connect(merged_db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM UserMark")
            total = cursor.fetchone()[0]
            print(f"UserMarks dans la DB: {total}")
            cursor.execute("""
                SELECT ColorIndex, StyleIndex, COUNT(*) 
                FROM UserMark 
                GROUP BY ColorIndex, StyleIndex
            """)
            print("Répartition par couleur/style:")
            for color, style, count in cursor.fetchall():
                print(f"- Couleur {color}, Style {style}: {count} marques")

        print(f"Location IDs mappés: {location_id_map}")
        print(f"UserMark GUIDs mappés: {usermark_guid_map}")

        # ===== Vérification pré-fusion complète =====
        print("\n=== VERIFICATION PRE-FUSION ===")
        print("\n[VÉRIFICATION FICHIERS SOURCES]")
        source_files = [
            os.path.join(EXTRACT_FOLDER, "file1_extracted", "userData.db"),
            os.path.join(EXTRACT_FOLDER, "file2_extracted", "userData.db")
        ]
        for file in source_files:
            print(f"Vérification {file}... ", end="")
            if not os.path.exists(file):
                print("ERREUR: Fichier manquant")
                return jsonify({"error": f"Fichier source manquant: {file}"}), 400
            else:
                print(f"OK ({os.path.getsize(file) / 1024:.1f} KB)")

        print("\n[VÉRIFICATION SCHÉMA]")

        def verify_schema(db_path):
            try:
                conn = sqlite3.connect(db_path)
                cursor = conn.cursor()
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
                tables = [t[0] for t in cursor.fetchall()]
                print(f"Tables dans {os.path.basename(db_path)}: {len(tables)}")
                required_tables = {'Bookmark', 'Location', 'UserMark', 'Note'}
                missing = required_tables - set(tables)
                if missing:
                    print(f"  TABLES MANQUANTES: {missing}")
                conn.close()
                return not bool(missing)
            except Exception as e:
                print(f"  ERREUR: {str(e)}")
                return False
        if not all(verify_schema(db) for db in source_files):
            return jsonify({"error": "Schéma de base de données incompatible"}), 400

        print("\n[VÉRIFICATION BASE DE DESTINATION]")
        print(f"Vérification {merged_db_path}... ", end="")
        try:
            conn = sqlite3.connect(merged_db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = [t[0] for t in cursor.fetchall()]
            print(f"OK ({len(tables)} tables)")
            conn.close()
        except Exception as e:
            print(f"ERREUR: {str(e)}")
            return jsonify({"error": "Base de destination corrompue"}), 500

        print("\n[VÉRIFICATION SYSTÈME]")
        try:
            import psutil
            mem = psutil.virtual_memory()
            print(f"Mémoire disponible: {mem.available / 1024 / 1024:.1f} MB")
            if mem.available < 500 * 1024 * 1024:
                print("ATTENTION: Mémoire insuffisante")
        except ImportError:
            print("psutil non installé - vérification mémoire ignorée")

        print("\n=== PRÊT POUR FUSION ===\n")

        try:
            merge_bookmarks(
                merged_db_path,
                file1_db,
                file2_db,
                location_id_map,
                choix_marque_pages_client  # ✨ UTILISEZ la variable que nous avons définie plus haut
            )

        except Exception as e:
            import traceback
            print(f"❌ Erreur dans merge_bookmarks : {e}")
            traceback.print_exc()
            raise

        # --- FUSION BLOCKRANGE ---
        print("\n=== DEBUT FUSION BLOCKRANGE ===")
        try:
            if not merge_blockrange_from_two_sources(merged_db_path, file1_db, file2_db):
                print("ÉCHEC Fusion BlockRange")
                return jsonify({"error": "BlockRange merge failed"}), 500
        except Exception as e:
            import traceback
            print(f"❌ Erreur dans merge_blockrange_from_two_sources : {e}")
            traceback.print_exc()
            raise

        # Mapping inverse UserMarkId original → nouveau
        usermark_guid_map = {}
        conn = sqlite3.connect(merged_db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT UserMarkId, UserMarkGuid FROM UserMark")
        for new_id, guid in cursor.fetchall():
            usermark_guid_map[guid] = new_id
        conn.close()

        # --- Avant fusion Tags et TagMap, on affiche note_mapping ---
        print("📦 Avant merge_tags_and_tagmap (1) :")
        print(f"🔢 note_mapping contient {len(note_mapping)} entrées")
        print("🔢 Clés note_mapping (extraits) :", list(note_mapping.keys())[:10])

        # --- Étape 2 : Fusion des Notes avec les nouveaux TagIds ---
        try:
            note_mapping = merge_notes(
                merged_db_path,
                file1_db,
                file2_db,
                location_id_map,
                usermark_guid_map,
                payload.get("choices", {}).get("notes", {}),
                tag_id_map  # ✅ ici maintenant
            )
        except Exception as e:
            import traceback
            print(f"❌ Erreur dans merge_notes : {e}")
            traceback.print_exc()
            raise

        print(f"🔢 note_mapping size before tag merge: {len(note_mapping)}", flush=True)
        sample_keys = list(note_mapping.items())[:10]
        print(f"🔢 Sample note_mapping entries: {sample_keys}", flush=True)

        # --- Étape 1 : Fusion des Tags et TagMap (pour obtenir tag_id_map) ---
        try:
            print("🐞 [CALLING merge_tags_and_tagmap]")
            tag_id_map, tagmap_id_map = merge_tags_and_tagmap(
                merged_db_path,
                file1_db,
                file2_db,
                note_mapping,  # <-- on passe la vraie variable ici
                location_id_map,
                item_id_map,
                payload.get("choices", {}).get("tags", {})
            )
        except Exception as e:
            import traceback
            print("❌ Échec de merge_tags_and_tagmap (mais on continue le merge global) :")
            print(f"Exception capturée : {e}")
            traceback.print_exc()
            tag_id_map, tagmap_id_map = {}, {}

        # --- Étape 3 : Réappliquer les catégories manuelles sélectionnées ---
        apply_selected_tags(
            merged_db_path,
            file1_db,
            file2_db,
            payload.get("choices", {}).get("notes", {}),
            note_mapping,
            tag_id_map
        )

        # --- Debug facultatif ---
        print(f"Tag ID Map: {tag_id_map}")
        print(f"TagMap ID Map: {tagmap_id_map}")

        try:
            with sqlite3.connect(merged_db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM Tag")
                tags_count = cursor.fetchone()[0]
                cursor.execute("SELECT COUNT(*) FROM TagMap")
                tagmaps_count = cursor.fetchone()[0]
                print(f"Tags: {tags_count}")
                print(f"TagMaps: {tagmaps_count}")
                cursor.execute("""
                    SELECT COUNT(*) 
                    FROM TagMap 
                    WHERE NoteId NOT IN (SELECT NoteId FROM Note)
                """)
                orphaned = cursor.fetchone()[0]
                print(f"TagMaps orphelins: {orphaned}")
        except Exception as e:
            print(f"❌ ERREUR dans la vérification des tags : {e}")
            import traceback
            traceback.print_exc()
            return jsonify({"error": "Erreur lors de la vérification des tags"}), 500

        print("\n▶️ Début de la fusion des éléments liés aux playlists...")

        print("\n▶️ Fusion des éléments liés aux playlists terminée.")

        # ─── Avant merge_other_tables ────────────────────────────────────────────
        tables_to_check = [
            'PlaylistItem',
            'IndependentMedia',
            'PlaylistItemLocationMap',
            'PlaylistItemIndependentMediaMap'
        ]
        print("\n--- COMPTES AVANT merge_other_tables ---")
        with sqlite3.connect(merged_db_path) as dbg_conn:
            dbg_cur = dbg_conn.cursor()
            for tbl in tables_to_check:
                dbg_cur.execute(f"SELECT COUNT(*) FROM {tbl}")
                cnt_merged = dbg_cur.fetchone()[0]

                dbg_cur.execute(f"ATTACH DATABASE ? AS src1", (file1_db,))
                dbg_cur.execute(f"SELECT COUNT(*) FROM src1.{tbl}")
                cnt1 = dbg_cur.fetchone()[0]
                dbg_cur.execute("DETACH DATABASE src1")

                dbg_cur.execute(f"ATTACH DATABASE ? AS src2", (file2_db,))
                dbg_cur.execute(f"SELECT COUNT(*) FROM src2.{tbl}")
                cnt2 = dbg_cur.fetchone()[0]
                dbg_cur.execute("DETACH DATABASE src2")

                print(f"[AVANT ] {tbl}: merged={cnt_merged}, file1={cnt1}, file2={cnt2}")

        # Fermer toutes les connexions avant les appels suivants
        try:
            merge_other_tables(
                merged_db_path,
                file1_db,
                file2_db,
                exclude_tables=[
                    'Note', 'UserMark', 'Location', 'BlockRange',
                    'LastModified', 'Tag', 'TagMap', 'PlaylistItem',
                    'InputField', 'Bookmark', 'android_metadata', 'grdb_migrations'
                ]
            )
        except Exception as e:
            import traceback
            print(f"❌ Erreur dans merge_other_tables : {e}")
            traceback.print_exc()
            raise

        merge_platform_metadata(merged_db_path, file1_db, file2_db)

        # ─── Après merge_other_tables ───────────────────────────────────────────
        print("\n--- COMPTES APRÈS merge_other_tables ---")
        with sqlite3.connect(merged_db_path) as dbg_conn:
            dbg_cur = dbg_conn.cursor()
            for tbl in tables_to_check:
                dbg_cur.execute(f"SELECT COUNT(*) FROM {tbl}")
                cnt_merged = dbg_cur.fetchone()[0]

                dbg_cur.execute(f"ATTACH DATABASE ? AS src1", (file1_db,))
                dbg_cur.execute(f"SELECT COUNT(*) FROM src1.{tbl}")
                cnt1 = dbg_cur.fetchone()[0]
                dbg_cur.execute("DETACH DATABASE src1")

                dbg_cur.execute(f"ATTACH DATABASE ? AS src2", (file2_db,))
                dbg_cur.execute(f"SELECT COUNT(*) FROM src2.{tbl}")
                cnt2 = dbg_cur.fetchone()[0]
                dbg_cur.execute("DETACH DATABASE src2")

                print(f"[APRÈS] {tbl}: merged={cnt_merged}, file1={cnt1}, file2={cnt2}")

        # 8. Vérification finale des thumbnails
        print("\n[VÉRIFICATION THUMBNAILS ORPHELINS]")
        cursor.execute("""
                    SELECT p.PlaylistItemId, p.ThumbnailFilePath
                    FROM PlaylistItem p
                    WHERE p.ThumbnailFilePath IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM IndependentMedia m 
                          WHERE m.FilePath = p.ThumbnailFilePath
                      )
                """)
        orphaned_thumbnails = cursor.fetchall()
        if orphaned_thumbnails:
            print(f"Avertissement : {len(orphaned_thumbnails)} thumbnails sans média associé")

        # 9. Finalisation playlists
        print("\n=== FUSION PLAYLISTS TERMINÉE ===")
        playlist_results = {
            'item_id_map': item_id_map,
            'marker_id_map': marker_id_map,
            'media_status': {
                'total_media': max_media_id,
                'orphaned_thumbnails': len(orphaned_thumbnails) if 'orphaned_thumbnails' in locals() else 0
            }
        }
        print(f"Résumé intermédiaire: {playlist_results}")

        # 11. Vérification de cohérence
        print("\n=== VERIFICATION COHERENCE ===")
        cursor.execute("""
            SELECT COUNT(*) 
              FROM PlaylistItem pi
             WHERE pi.PlaylistItemId NOT IN (
                    SELECT PlaylistItemId FROM PlaylistItemLocationMap
                    UNION
                    SELECT PlaylistItemId FROM PlaylistItemIndependentMediaMap
                )
        """)
        orphaned_items = cursor.fetchone()[0]
        status_color = "\033[91m" if orphaned_items > 0 else "\033[92m"
        print(f"{status_color}Éléments sans parent détectés (non supprimés) : {orphaned_items}\033[0m")

        # 12. Suppression des PlaylistItem orphelins
        with sqlite3.connect(merged_db_path) as conn_del:
            cur = conn_del.cursor()
            cur.execute("""
                DELETE FROM PlaylistItem
                 WHERE PlaylistItemId NOT IN (
                    SELECT PlaylistItemId FROM PlaylistItemLocationMap
                    UNION
                    SELECT PlaylistItemId FROM PlaylistItemIndependentMediaMap
                 )
            """)
            conn_del.commit()
        print("→ PlaylistItem orphelins supprimés")

        # 13. Optimisations finales
        print("\n=== DEBUT OPTIMISATIONS ===")

        # Définition de log_message **avant** son premier appel
        log_file = os.path.join(UPLOAD_FOLDER, "fusion.log")

        def log_message(message, log_type="INFO"):
            print(message)
            with open(log_file, "a") as f:
                f.write(f"[{log_type}] {datetime.now().strftime('%H:%M:%S')} - {message}\n")

        # 13.1 Reconstruction des index
        print("\nReconstruction des index...")
        cursor.execute("SELECT name FROM sqlite_master WHERE type='index'")
        indexes = [row[0] for row in cursor.fetchall() if not row[0].startswith('sqlite_autoindex_')]
        for index_name in indexes:
            try:
                cursor.execute(f"REINDEX {index_name}")
                log_message(f"Index reconstruit: {index_name}")
            except sqlite3.Error as e:
                log_message(f"ERREUR sur index {index_name}: {str(e)}", "ERROR")

        # 13.2 Vérification intégrité
        print("\nVérification intégrité base de données...")
        cursor.execute("PRAGMA quick_check")
        integrity_result = cursor.fetchone()[0]
        if integrity_result == "ok":
            log_message("Intégrité de la base: OK")
        else:
            log_message(f"ERREUR intégrité: {integrity_result}", "ERROR")

        # 13.3 Vérification clés étrangères
        cursor.execute("PRAGMA foreign_key_check")
        fk_issues = cursor.fetchall()
        if fk_issues:
            log_message(f"ATTENTION: {len(fk_issues)} problèmes de clés étrangères", "WARNING")
            for issue in fk_issues[:3]:
                log_message(f"- Problème: {issue}", "WARNING")
        else:
            log_message("Aucun problème de clé étrangère détecté")

        # --- 14. Finalisation ---
        # commit final et fermeture propre de la transaction playlists
        conn.commit()

        # Récapitulatif final
        print("\n=== RÉCAPITULATIF FINAL ===")
        print(f"{'Playlists:':<20}, {max_playlist_id}")
        print(f"{'Éléments:':<20}, {len(item_id_map)}")
        print(f"{'Médias:':<20}, {max_media_id}")
        print(f"{'Nettoyés:':<20}, {orphaned_deleted}")
        print(f"{'Intégrité:':<20}, {integrity_result}")
        if fk_issues:
            print(f"{'Problèmes FK:':<20} \033[91m{len(fk_issues)}\033[0m")
        else:
            print(f"{'Problèmes FK:':<20} \033[92mAucun\033[0m")

        # 16. Activation du WAL
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("CREATE TABLE IF NOT EXISTS dummy_for_wal (id INTEGER PRIMARY KEY)")
        cursor.execute("INSERT INTO dummy_for_wal DEFAULT VALUES")
        cursor.execute("DELETE FROM dummy_for_wal")
        cursor.execute("DROP TABLE dummy_for_wal")
        conn.commit()
        conn.close()

        # Vérification du mode WAL
        with sqlite3.connect(merged_db_path) as test_conn:
            new_wal_status = test_conn.execute("PRAGMA journal_mode").fetchone()[0]
            print(f"Statut WAL après activation: {new_wal_status}")
            if new_wal_status != "wal":
                print("Avertissement: Échec de l'activation WAL")

        print("📍 Avant le résumé final")

        print("▶️ Appel de merge_playlists...")
        print("🛑 merge_playlists appelée")

        try:
            result = merge_playlists(
                merged_db_path,
                file1_db,
                file2_db,
                location_id_map,
                independent_media_map,
                item_id_map  # ⚠️ on passe le dict déjà défini (pas un nouveau {})
            )

            # 🔄 mise à jour propre des variables
            (
                max_playlist_id,
                playlist_item_total,
                max_media_id,
                orphaned_deleted,
                integrity_result,
                item_id_map
            ) = result

            print("\n🔍 Vérification spécifique de item_id_map pour PlaylistItemId 1 et 2")

            for test_id in [1, 2]:
                for db in [file1_db, file2_db]:
                    key = (db, test_id)
                    found = item_id_map.get(key)
                    print(f"  {key} → {found}")

            # 🧪 Résumé post merge_playlists
            print("\n🎯 Résumé final après merge_playlists:")
            print(f"- Playlists max ID: {max_playlist_id}")
            print(f"- PlaylistItem total: {playlist_item_total}")
            print(f"- Médias max ID: {max_media_id}")
            print(f"- Orphelins supprimés: {orphaned_deleted}")
            print(f"- Résultat intégrité: {integrity_result}")
            print("✅ Tous les calculs terminés, nettoyage…")

            print("item_id_map keys:", list(item_id_map.keys()))
            print("location_id_map keys:", list(location_id_map.keys()))
            print("note_mapping keys:", list(note_mapping.keys()))

            print("📦 Vérification complète de item_id_map AVANT merge_tags_and_tagmap:")
            for (db_path, old_id), new_id in item_id_map.items():
                print(f"  FROM {db_path} - OldID: {old_id} → NewID: {new_id}")

            print("🧪 CONTENU DE item_id_map APRÈS merge_playlists:")
            for k, v in item_id_map.items():
                print(f"  {k} → {v}")

            # --- Avant fusion Tags et TagMap, on affiche note_mapping ---
            print("📦 Avant merge_tags_and_tagmap (2) :")
            print(f"🔢 note_mapping contient {len(note_mapping)} entrées")
            print("🔢 Clés note_mapping (extraits) :", list(note_mapping.keys())[:10])

            # --- Étape 1 : fusion des Tags et TagMap (utilise location_id_map) ---
            try:
                tag_id_map, tagmap_id_map = merge_tags_and_tagmap(
                    merged_db_path,
                    file1_db,
                    file2_db,
                    note_mapping,
                    location_id_map,
                    item_id_map,
                    payload.get("choices", {}).get("tags", {})
                )
                print(f"✔ merge_tags_and_tagmap réussi :")
                print(f"  Tag ID Map contient {len(tag_id_map)} entrées")
                print(f"  TagMap ID Map contient {len(tagmap_id_map)} entrées")
            except Exception as e:
                import traceback
                print("❌ Échec de merge_tags_and_tagmap (mais on continue le merge global) :")
                print(f"Exception capturée : {e}")
                traceback.print_exc()
                tag_id_map, tagmap_id_map = {}, {}

            print(f"Tag ID Map: {tag_id_map}")
            print(f"TagMap ID Map: {tagmap_id_map}")

            # 1️⃣ Mise à jour des LocationId résiduels
            print("\n=== MISE À JOUR DES LocationId RÉSIDUELS ===")
            merge_inputfields(merged_db_path, file1_db, file2_db, location_id_map)
            print("✔ Fusion InputFields terminée")
            location_replacements_flat = {
                old_id: new_id
                for (_, old_id), new_id in sorted(location_id_map.items())
            }

            print("⏳ Appel de update_location_references...")
            try:
                update_location_references(merged_db_path, location_replacements_flat)
                print("✔ Mise à jour des références LocationId terminée")
            except Exception as e:
                import traceback
                print(f"❌ ERREUR dans update_location_references : {e}")
                traceback.print_exc()

            with sqlite3.connect(merged_db_path) as conn:
                cleanup_playlist_item_location_map(conn)

            print("🟡 Après update_location_references")
            sys.stdout.flush()
            time.sleep(0.5)
            print("🟢 Avant suppression des tables MergeMapping_*")

            # 2️⃣ Suppression des tables MergeMapping_*
            print("\n=== SUPPRESSION DES TABLES MergeMapping_* ===")
            with sqlite3.connect(merged_db_path) as cleanup_conn:
                cleanup_conn.execute("PRAGMA busy_timeout = 5000")
                cur = cleanup_conn.cursor()
                cur.execute("""
                    SELECT name
                    FROM sqlite_master
                    WHERE type='table'
                      AND LOWER(name) LIKE 'mergemapping_%'
                """)
                rows = cur.fetchall()
                tables_to_drop = [row[0] for row in rows]
                print(f"🧪 Résultat brut de la requête sqlite_master : {rows}")
                print(f"🧹 Tables MergeMapping_ détectées : {tables_to_drop}")
                for tbl in tables_to_drop:
                    cur.execute(f"DROP TABLE IF EXISTS {tbl}")
                    print(f"✔ Table supprimée : {tbl}")
                cleanup_conn.commit()

            # 🔍 Vérification juste avant la copie
            print("📄 Vérification taille et date de merged_userData.db juste avant la copie")
            print("📍 Fichier:", merged_db_path)
            print("🕒 Modifié le:", os.path.getmtime(merged_db_path))
            print("📦 Taille:", os.path.getsize(merged_db_path), "octets")
            with sqlite3.connect(merged_db_path) as check_conn:
                cur = check_conn.cursor()
                cur.execute("SELECT name FROM sqlite_master WHERE name LIKE 'MergeMapping_%'")
                leftover = [row[0] for row in cur.fetchall()]
                print(f"🧪 Tables restantes juste avant la copie (vérification finale): {leftover}")

            print("🧹 Libération mémoire et attente...")
            gc.collect()
            time.sleep(1.0)

            with sqlite3.connect(merged_db_path) as conn:
                conn.execute("DROP TABLE IF EXISTS PlaylistItemMediaMap")
                print("🗑️ Table PlaylistItemMediaMap supprimée avant VACUUM.")

            # 6️⃣ Création d’une DB propre avec VACUUM INTO
            clean_filename = f"cleaned_{uuid.uuid4().hex}.db"
            clean_path = os.path.join(UPLOAD_FOLDER, clean_filename)

            print("🧹 VACUUM INTO pour générer une base nettoyée...")
            with sqlite3.connect(merged_db_path) as conn:
                conn.execute(f"VACUUM INTO '{clean_path}'")
            print(f"✅ Fichier nettoyé généré : {clean_path}")

            # 🧪 Création d'une copie debug (juste pour toi)
            debug_copy_path = os.path.join(UPLOAD_FOLDER, "debug_cleaned_before_copy.db")
            shutil.copy(clean_path, debug_copy_path)
            print(f"📤 Copie debug créée : {debug_copy_path}")



            # 7️⃣ Copie vers destination finale officielle pour le frontend
            # ⛔ final_db_dest = os.path.join(UPLOAD_FOLDER, "userData.db")
            # ⛔ shutil.copy(clean_path, final_db_dest)
            # ⛔ print(f"✅ Copie finale pour frontend : {final_db_dest}")

            # ✅ On force l’usage uniquement du fichier debug (3 lignes d'ajout pour n'envoyer que le fichier)
            final_db_dest = os.path.join(UPLOAD_FOLDER, "debug_cleaned_before_copy.db")
            print("🚫 Copie vers userData.db désactivée — envoi direct de debug_cleaned_before_copy.db")



            # ✅ Forcer la génération des fichiers WAL et SHM sur userData.db
            try:
                print("🧪 Activation du mode WAL pour générer les fichiers -wal et -shm sur userData.db...")
                with sqlite3.connect(final_db_dest) as conn:
                    conn.execute("PRAGMA journal_mode=WAL;")
                    conn.execute("CREATE TABLE IF NOT EXISTS _Dummy (x INTEGER);")
                    conn.execute("INSERT INTO _Dummy (x) VALUES (1);")
                    conn.execute("DELETE FROM _Dummy;")
                    conn.execute("DROP TABLE IF EXISTS _Dummy;")  # Suppression finale
                    conn.commit()
                print("✅ WAL/SHM générés et _Dummy supprimée sur userData.db")
            except Exception as e:
                print(f"❌ Erreur WAL/SHM sur userData.db: {e}")

            # 8️⃣ Vérification finale dans userData.db
            with sqlite3.connect(final_db_dest) as final_check:
                cur = final_check.cursor()
                cur.execute("SELECT name FROM sqlite_master WHERE name LIKE 'MergeMapping_%'")
                tables_final = [row[0] for row in cur.fetchall()]
                # print("📋 Tables MergeMapping_ dans userData.db copié :", tables_final)
                print("📋 Tables MergeMapping_ dans debug_cleaned_before_copy.db :", tables_final)

            # À la toute fin, juste avant return
            os.remove(os.path.join(UPLOAD_FOLDER, "merge_in_progress"))

            elapsed = time.time() - start_time
            print(f"⏱️ Temps total du merge : {elapsed:.2f} secondes")

            # 5️⃣ Retour JSON final
            final_result = {
                "merged_file": "debug_cleaned_before_copy.db",
                "playlists": max_playlist_id,
                "merge_status": "done",
                "playlist_items": playlist_item_total,
                "media_files": max_media_id,
                "cleaned_items": orphaned_deleted,
                "integrity_check": integrity_result
            }
            sys.stdout.flush()
            print("🎯 Résumé final prêt à être envoyé au frontend.")
            print("🧪 Test accès à final_result:", final_result)
            return jsonify(final_result), 200

        except Exception as e:
            import traceback
            print("❌ Exception levée pendant merge_data !")
            traceback.print_exc()
            return jsonify({"error": f"Erreur dans merge_data: {str(e)}"}), 500

    finally:
        if conn:
            try:
                conn.close()
            except:
                pass


# === 🔒 Ancienne méthode de génération ZIP backend (désactivée avec JSZip) ===

# def create_userdata_zip():
#     print("🧹 Création du zip userData_only.zip après merge terminé...")
#
#     zip_filename = "userData_only.zip"
#     zip_path = os.path.join(UPLOAD_FOLDER, zip_filename)
#
#     debug_db_path = os.path.join(UPLOAD_FOLDER, "debug_cleaned_before_copy.db")
#     shm_path = debug_db_path + "-shm"
#     wal_path = debug_db_path + "-wal"
#
#     with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_STORED) as zipf:
#         zipf.write(debug_db_path, arcname="userData.db")
#         if os.path.exists(shm_path):
#             zipf.write(shm_path, arcname="userData.db-shm")
#         if os.path.exists(wal_path):
#             zipf.write(wal_path, arcname="userData.db-wal")
#
#     print(f"✅ Fichier ZIP final prêt : {zip_path}")
#
#
# @app.route("/create_zip_after_merge")
# def create_zip_after_merge():
#     start_time = time.time()
#
#     try:
#         create_userdata_zip()
#
#         # 🔐 Suppression du verrou juste après création du ZIP
#         try:
#             os.remove(os.path.join(UPLOAD_FOLDER, "merge_in_progress"))
#             print("🧹 Verrou merge_in_progress supprimé après création ZIP.")
#         except FileNotFoundError:
#             print("⚠️ Aucun verrou à supprimer : merge_in_progress absent.")
#
#         elapsed = time.time() - start_time
#         print(f"📦 Temps de création du ZIP : {elapsed:.2f} secondes")
#
#         return jsonify({"status": "ZIP créé avec succès"}), 200
#
#     except Exception as e:
#         print(f"❌ Erreur création ZIP : {e}")
#         return jsonify({"error": str(e)}), 500
#
#
# @app.route("/download_userdata_zip")
# def download_userdata_zip():
#     zip_path = os.path.join(UPLOAD_FOLDER, "userData_only.zip")
#     if not os.path.exists(zip_path):
#         return jsonify({"error": "Fichier ZIP introuvable"}), 404
#     print(f"📥 Envoi du ZIP : {zip_path}")
#     return send_file(zip_path, as_attachment=True, download_name="userData_only.zip")


@app.route("/download_debug_db")
def download_debug_db():
    debug_path = os.path.join(UPLOAD_FOLDER, "debug_cleaned_before_copy.db")
    if not os.path.exists(debug_path):
        return jsonify({"error": "Fichier debug introuvable"}), 404
    print(f"📥 Envoi du fichier DEBUG : {debug_path}")
    return send_file(debug_path, as_attachment=True, download_name="userData.db")


@app.route("/download/debug")
def download_debug_copy():
    path = os.path.join(UPLOAD_FOLDER, "debug_cleaned_before_copy.db")
    if not os.path.exists(path):
        return jsonify({"error": "Fichier debug non trouvé"}), 404
    return send_file(path, as_attachment=True, download_name="debug_cleaned_before_copy.db")


@app.route("/download/<filename>")
def download_file(filename):
    if os.path.exists(os.path.join(UPLOAD_FOLDER, "merge_in_progress")):
        print("🛑 Tentative de téléchargement bloquée : merge encore en cours.")
        return jsonify({"error": "Le fichier est encore en cours de création"}), 503

    # allowed_files = {"userData.db", "userData.db-shm", "userData.db-wal"}
    allowed_files = {
        "debug_cleaned_before_copy.db",
        "debug_cleaned_before_copy.db-shm",
        "debug_cleaned_before_copy.db-wal"
    }

    if filename not in allowed_files:
        return jsonify({"error": "Fichier non autorisé"}), 400

    path = os.path.join(UPLOAD_FOLDER, filename)
    if not os.path.exists(path):
        return jsonify({"error": "Fichier introuvable"}), 404

    print(f"📥 Envoi du fichier : {filename}")
    response = send_file(path, as_attachment=True)
    response.headers.add("Access-Control-Allow-Origin", "*")
    return response


@app.errorhandler(Exception)
def handle_exception(e):
    response = jsonify({"error": str(e)})
    response.headers.add("Access-Control-Allow-Origin", "*")
    return response, 500


import json
from flask import Response


@app.route("/")
def index():
    message = {"message": "Le serveur Flask fonctionne 🎉"}
    return Response(
        response=json.dumps(message, ensure_ascii=False),
        status=200,
        mimetype='application/json'
    )


# Fichier local pour stocker les stats
MERGE_STATS_FILE = "merge_stats.json"
STATS_LOCK = threading.Lock()


import json
import os


def load_merge_stats():
    if not os.path.exists(MERGE_STATS_FILE):
        return {"success": 0, "error": 0}
    with open(MERGE_STATS_FILE, "r") as f:
        return json.load(f)


def save_merge_stats(stats):
    with open(MERGE_STATS_FILE, "w") as f:
        json.dump(stats, f)


@app.route("/track-merge", methods=["POST"])
def track_merge():
    try:
        data = request.get_json()
        status = data.get("status")
        if status not in ("success", "error"):
            return jsonify({"error": "Invalid status"}), 400

        with STATS_LOCK:
            stats = load_merge_stats()
            if status == "error":
                error_message = data.get("message", "Erreur inconnue")
                if "errors" not in stats:
                    stats["errors"] = []
                stats["errors"].append(error_message)
                stats["error"] = stats.get("error", 0) + 1
            else:
                stats["success"] = stats.get("success", 0) + 1

            save_merge_stats(stats)

        return jsonify({"message": f"{status} count updated"}), 200
    except Exception as e:
        print("❌ Erreur dans /track-merge :", e)
        return jsonify({"error": str(e)}), 500


@app.route("/get-merge-stats", methods=["GET"])
def get_merge_stats():
    with STATS_LOCK:
        stats = load_merge_stats()

    return Response(
        response=json.dumps(stats, ensure_ascii=False, indent=2),
        status=200,
        mimetype='application/json'
    )


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8000))
    app.run(host='0.0.0.0', port=port)

