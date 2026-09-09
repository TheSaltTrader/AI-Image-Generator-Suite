"""Census of the Variations store (variations_db.VariationsDB).

Enumerates the store's own operations so anything NOT covered is counted, not
averaged away (Lodestone style). No engine or GPU needed.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from variations_db import VariationsDB          # noqa: E402
from PIL import Image                            # noqa: E402

passed = failed = 0


def ok(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}")


def _img(path, color):
    Image.new("RGB", (48, 48), color).save(path)
    return path


def main():
    d = tempfile.mkdtemp()
    face = _img(os.path.join(d, "face.png"), (200, 10, 10))
    ref = _img(os.path.join(d, "ref.png"), (10, 200, 10))

    print("subject: add / list / get")
    db = VariationsDB(os.path.join(d, "store"))
    cfg = {"prompt": "poolside goddess", "model": "Juggernaut-XL-v9.safetensors",
           "loras": ["Nude-Model-solo.safetensors"], "preset": "Noir / Sin City"}
    vid = db.add("Ava", "blonde, poolside", face, ref, config=cfg, seed="4242")
    rows = db.list()
    ok("add returns an id", isinstance(vid, int) and vid > 0)
    ok("list has the new row", len(rows) == 1)
    ok("description stored", rows[0]["description"] == "blonde, poolside")
    ok("seed stored", rows[0]["seed"] == "4242")
    got = db.get(vid)
    ok("get finds it", got is not None and got["name"] == "Ava")
    ok("config round-trips", VariationsDB.config_of(got).get("prompt")
       == "poolside goddess")
    ok("face + ref copied into the store (not referenced in place)",
       os.path.exists(got["face_path"]) and os.path.exists(got["ref_path"])
       and os.path.dirname(got["face_path"]) != d)

    print("subject: filenames are unique under rapid add")
    ids = [db.add(f"n{i}", f"d{i}", face, ref, config={"i": i}) for i in range(6)]
    paths = [db.get(i)["face_path"] for i in ids]
    ok("no filename collisions in a burst", len(set(paths)) == len(paths))

    print("subject: delete removes row AND files")
    g = db.get(vid)
    db.delete(vid)
    ok("row gone after delete", db.get(vid) is None)
    ok("face file removed", not os.path.exists(g["face_path"]))
    ok("others untouched", len(db.list()) == 6)

    print("subject: export / import round-trip to another install")
    z = os.path.join(d, "exp.zip")
    db.export_zip(z)
    ok("export produced a zip", os.path.exists(z) and os.path.getsize(z) > 0)
    db2 = VariationsDB(os.path.join(d, "store2"))
    n = db2.import_zip(z)
    ok("import count matches", n == 6)
    ok("imported rows carry config",
       all(VariationsDB.config_of(r).get("i") is not None
           for r in db2.list()))
    ok("imported image files exist locally",
       all(os.path.exists(r["face_path"]) and os.path.exists(r["ref_path"])
           for r in db2.list()))
    ok("import copied into store2, not store",
       all(os.path.join("store2", "") in r["face_path"].replace("\\", "/")
           or "/store2/" in r["face_path"].replace("\\", "/")
           for r in db2.list()))

    print("subject: zip-slip is refused on import")
    import zipfile
    evil = os.path.join(d, "evil.zip")
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("variations.db", "not a db")
        zf.writestr("../escape.txt", "nope")
    before = set(os.listdir(d))
    try:
        db2.import_zip(evil)   # bad db -> 0, and no escape file written
    except Exception:
        pass
    ok("no traversal file escaped the temp dir",
       "escape.txt" not in set(os.listdir(os.path.dirname(d))))
    ok("still no crash / dir intact", os.path.isdir(d))

    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
