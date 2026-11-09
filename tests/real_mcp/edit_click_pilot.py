"""Disposable manual local click pilot; NO remote file-edit tool."""
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
from sora_commander_next.file_edits import review_and_replace_local

def main():
    with TemporaryDirectory(prefix='sora-next-edit-review-') as folder:
        target = Path(folder) / 'review-example.txt'
        original = b'Before Owner review\n'
        target.write_bytes(original)
        print('BEFORE=' + target.read_text(encoding='utf-8').strip(), flush=True)
        result = review_and_replace_local(str(target), (str(Path(folder).resolve()),),
                                          'After Owner review\n', sha256(original).hexdigest())
        print('REVIEW_RESULT=' + result['status'], flush=True)
        print('AFTER=' + target.read_text(encoding='utf-8').strip(), flush=True)
        print('EXPECTED=' + ('After Owner review' if result['status']=='REPLACED_LOCAL_DEV' else 'Before Owner review'),flush=True)

if __name__ == '__main__':
    main()
