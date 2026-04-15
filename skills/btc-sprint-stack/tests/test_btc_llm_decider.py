from __future__ import annotations

import sys
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[3]
MODULE_DIR = ROOT / 'skills' / 'btc-sprint-stack' / 'modules'
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

from btc_llm_decider import _extract_response_text  # noqa: E402


class ExtractResponseTextTests(unittest.TestCase):
    def test_rejects_truncated_plain_text_fragment(self) -> None:
        raw = {
            'choices': [
                {
                    'message': {
                        'content': 'Here is the JSON re',
                    }
                }
            ]
        }
        self.assertIsNone(_extract_response_text(raw))


if __name__ == '__main__':
    unittest.main()
