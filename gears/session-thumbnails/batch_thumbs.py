"""Queue session-thumbnails on every session of a project (dry run unless you answer y or pass --go).
Skips QA-* subjects and sessions that already have a session-thumbnails analysis of this gear version.

  python3 batch_thumbs.py "dantest/Lung Cancer Immunotherapy (Anti-PD-1_Lung)"
  python3 batch_thumbs.py "globallogic/Sandbox" --go
"""
import getpass, sys, flywheel

VERSION = "0.1.0"
try:
    fw = flywheel.Client()
except Exception:  # no CLI login in the workspace: ask once, never echo or store
    fw = flywheel.Client(getpass.getpass("Personal Flywheel API key (input hidden): ").strip())
print("as", fw.get_current_user().email)
path = next((x for x in sys.argv[1:] if not x.startswith("--")), "dantest/Lung Cancer Immunotherapy (Anti-PD-1_Lung)")
GO = "--go" in sys.argv
proj = fw.lookup(path)
gear = fw.lookup(f"gears/session-thumbnails/{VERSION}")
plan = []
for ses in proj.sessions.iter():
    subj = fw.get_subject(ses.subject.id).label
    if subj.startswith("QA-"):
        continue
    ses = ses.reload()
    if any(a.gear_info and a.gear_info.name == "session-thumbnails" and a.gear_info.version == VERSION for a in (ses.analyses or [])):
        print(f"{subj} / {ses.label}: already done"); continue
    plan.append((subj, ses))
    print(f"{subj} / {ses.label}")
if plan and (GO or input(f"Queue session-thumbnails {VERSION} on these {len(plan)} sessions? [y/N] ").strip().lower() == "y"):
    for subj, ses in plan:
        jid = gear.run(analysis_label=f"session-thumbnails {VERSION}", destination=ses, tags=["thumbnails"])
        print(f"   {subj} / {ses.label}: queued {jid}")
print("planned", len(plan))
