"""The 'Variations' store: the user's own saved people.

A Variation is a person the user liked in one generated image (usually one of
several variations a batch made from a prompt) and wants more of. It saves:
  * the full generation CONFIG that produced it (prompt, model, LoRA, RAG map,
    style, size, seed… — the whole recipe), so it can be recalled and run again
    to make MORE images of that person,
  * a face crop (for the face swap) and the full source image (an IP-Adapter
    reference for the whole-person look) — the identity anchor that keeps it the
    same person across new seeds,
  * a text description.

Everything lives under <project>/variations/ (a tiny SQLite index plus image
files), so it travels with the install and depends on nothing outside the app
folder. The image files are copied in, never referenced in place, so deleting
the original generated image never breaks a Variation.
"""
import json
import secrets
import shutil
import sqlite3
import time
import zipfile
from pathlib import Path


class VariationsDB:
    def __init__(self, root):
        self.dir = Path(root)
        self.img_dir = self.dir / "images"
        self.db_path = self.dir / "variations.db"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.img_dir.mkdir(parents=True, exist_ok=True)
        self._init()

    def _con(self):
        con = sqlite3.connect(str(self.db_path))
        con.row_factory = sqlite3.Row
        return con

    def _init(self):
        con = self._con()
        try:
            con.execute(
                """CREATE TABLE IF NOT EXISTS variations(
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       name TEXT NOT NULL,
                       description TEXT NOT NULL DEFAULT '',
                       face_path TEXT NOT NULL,
                       ref_path TEXT NOT NULL,
                       config TEXT NOT NULL DEFAULT '{}',
                       seed TEXT NOT NULL DEFAULT '',
                       created TEXT NOT NULL)""")
            con.commit()
        finally:
            con.close()

    def add(self, name, description, face_src, ref_src, config=None, seed=""):
        """Copy the face crop + full reference image into the store and index
        them with the generation config that made them. `face_src`/`ref_src`
        are paths to existing image files; `config` is a dict (the UI settings
        recipe). Returns the new row's id."""
        name = (name or "Variation").strip()[:80] or "Variation"
        # unique per row (bulk import in the same second must not collide)
        stamp = time.strftime("%Y%m%d_%H%M%S") + "_" + secrets.token_hex(4)
        face_dst = self.img_dir / f"{stamp}_face.png"
        ref_dst = self.img_dir / f"{stamp}_ref.png"
        shutil.copyfile(face_src, face_dst)
        shutil.copyfile(ref_src, ref_dst)
        con = self._con()
        try:
            cur = con.execute(
                "INSERT INTO variations(name, description, face_path, "
                "ref_path, config, seed, created) VALUES(?,?,?,?,?,?,?)",
                (name, (description or "").strip(), str(face_dst),
                 str(ref_dst), json.dumps(config or {}), str(seed), stamp))
            con.commit()
            return cur.lastrowid
        finally:
            con.close()

    @staticmethod
    def config_of(row):
        """Parse a row's stored config JSON back into a dict."""
        try:
            return json.loads(row.get("config") or "{}")
        except (ValueError, TypeError):
            return {}

    def list(self):
        """All variations, newest first, as a list of dict rows."""
        con = self._con()
        try:
            rows = con.execute(
                "SELECT * FROM variations ORDER BY id DESC").fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()

    def get(self, vid):
        con = self._con()
        try:
            r = con.execute("SELECT * FROM variations WHERE id=?",
                            (vid,)).fetchone()
            return dict(r) if r else None
        finally:
            con.close()

    def delete(self, vid):
        """Remove the row and its image files (best-effort on the files)."""
        row = self.get(vid)
        if not row:
            return
        con = self._con()
        try:
            con.execute("DELETE FROM variations WHERE id=?", (vid,))
            con.commit()
        finally:
            con.close()
        for k in ("face_path", "ref_path"):
            try:
                p = Path(row[k])
                if p.exists():
                    p.unlink()
            except OSError:
                pass

    def export_zip(self, dest_zip):
        """Bundle the whole store (index + images) into a .zip so the
        variations can be carried to another compatible install."""
        dest = Path(dest_zip)
        with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
            if self.db_path.exists():
                z.write(self.db_path, "variations.db")
            for p in sorted(self.img_dir.glob("*")):
                if p.is_file():
                    z.write(p, f"images/{p.name}")
        return dest

    def import_zip(self, src_zip):
        """Merge a variations export into this store: copy its images in under
        fresh local names and add its rows. Returns how many were imported.
        Safe to run more than once (each import adds copies)."""
        added = 0
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            with zipfile.ZipFile(src_zip) as z:
                # guard against zip-slip: only extract entries that stay inside
                for m in z.namelist():
                    if m.startswith(("/", "..")) or ".." in Path(m).parts:
                        continue
                    z.extract(m, tdp)
            tdb = tdp / "variations.db"
            if not tdb.exists():
                return 0
            con = sqlite3.connect(str(tdb))
            con.row_factory = sqlite3.Row
            try:
                rows = [dict(r) for r in con.execute(
                    "SELECT * FROM variations").fetchall()]
            finally:
                con.close()
            for r in rows:
                face = tdp / "images" / Path(r["face_path"]).name
                ref = tdp / "images" / Path(r["ref_path"]).name
                if not (face.exists() and ref.exists()):
                    continue
                self.add(name=r.get("name", "Variation"),
                         description=r.get("description", ""),
                         face_src=str(face), ref_src=str(ref),
                         config=self.config_of(r), seed=r.get("seed", ""))
                added += 1
        return added
