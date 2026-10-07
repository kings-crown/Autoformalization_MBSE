"""Every composed static generation/repair prompt agrees with process capacity."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CapacityPromptTests(unittest.TestCase):
    def test_default_and_expanded_capacity_match_prompts_and_validator(self):
        # Import-time settings must be checked in fresh processes. Inspect the
        # actual composed feedback prompt, including capability instructions,
        # so another stale nested quota cannot silently contradict the limit.
        code = r'''
import json, re, sys
sys.path.insert(0, 'scripts')
from canonical_cli import TLR_INSTRUCTIONS
from canonical_feedback import feedback_instructions
from canonical_patterns import INSTRUCTIONS
from canonical_repair import REPAIR_INSTRUCTIONS
from mutation_core import STATIC_MAX_VARIABLES, validate_context
from review_behavior import VARIABLE_NAME_GUIDE
prompts = {'initial': TLR_INSTRUCTIONS, 'feedback_frozen': feedback_instructions(False),
           'feedback_generated': feedback_instructions(True), 'abstention': REPAIR_INSTRUCTIONS,
           'capability': INSTRUCTIONS,
           'composed_feedback': feedback_instructions(True) + '\n' + TLR_INSTRUCTIONS}
counts = {name: [int(a or b) for a, b in re.findall(r'[Aa]t most (\d+) variables|\b(\d+)-symbol profile', text)]
          for name, text in prompts.items()}
try:
    validate_context([{'name': f'v{i}', 'type': 'Bool'} for i in range(25)], [])
    accepts_25 = True
except ValueError:
    accepts_25 = False
print(json.dumps({'capacity': STATIC_MAX_VARIABLES, 'prompt_limits': counts, 'accepts_25': accepts_25,
                  'variable_name_limits_match': 'at most 64 characters' in VARIABLE_NAME_GUIDE
                      and all(VARIABLE_NAME_GUIDE in text for text in prompts.values()),
                  'assumption_limits_unchanged': all('40 background assumptions' in text
                      for name, text in prompts.items() if name != 'capability')}))
'''
        for configured, expected in ((None, 24), ('64', 64)):
            with self.subTest(configured=configured):
                env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
                env.pop('MBSE_STATIC_MAX_VARIABLES', None)
                if configured is not None:
                    env['MBSE_STATIC_MAX_VARIABLES'] = configured
                completed = subprocess.run([sys.executable, '-c', code], cwd=ROOT, env=env,
                    check=True, capture_output=True, text=True, timeout=15)
                result = json.loads(completed.stdout)
                self.assertEqual(result['capacity'], expected)
                self.assertEqual(result['accepts_25'], expected >= 25)
                self.assertTrue(result['assumption_limits_unchanged'])
                self.assertTrue(result['variable_name_limits_match'])
                for name, limits in result['prompt_limits'].items():
                    self.assertTrue(limits, name)
                    self.assertEqual(set(limits), {expected}, name)


if __name__ == '__main__':
    unittest.main()
