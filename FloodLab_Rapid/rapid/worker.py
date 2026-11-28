import sys,json
from pathlib import Path
from .engine import run
if __name__=='__main__':
    p=Path(sys.argv[1])
    try:run(json.loads((p/'config.json').read_text()),p)
    except Exception as ex:
        (p/'error.json').write_text(json.dumps({'error':str(ex)[:1000]},ensure_ascii=False));sys.exit(1)
