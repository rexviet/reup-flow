import sys
from pathlib import Path
from localization.config import load_settings
from localization.storage.object_store import ObjectStore
from localization.review_bridge.repository import Repository
aweme, gen, dest = sys.argv[1], int(sys.argv[2]), Path(sys.argv[3])
store = ObjectStore.from_settings(load_settings())
repo = Repository(store, None)
_, project, _ = repo.current(aweme, gen)
print("head revision", project.revision_id, "cues", len(project.cues), "dur", project.duration_ms)
repo.export_revision(aweme, gen, project.revision_id, dest)
print("exported", len(list(dest.iterdir())))
