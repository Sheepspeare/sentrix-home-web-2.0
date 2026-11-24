"""Path migration checks: specific targets must override containing roots."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('layout',Path(__file__).with_name('layout.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class PathMappingTests(unittest.TestCase):
    def test_current_doubao_database_is_not_left_in_operation_directory(self):
        old='/home/realmagic/xy/experiments/atm1013-20261118/doubao-reuse-49f347c/product-data/sentrix.db'
        self.assertEqual(module.translate(old),'/home/realmagic/xy/benchmarks/atm-bench/instances/doubao/product-data/sentrix.db')

    def test_all_original_experiment_children_have_explicit_destinations(self):
        for row in module.PLAN['experiment_children']:
            old=module.PLAN['experiment_old']+'/'+row['name']
            self.assertEqual(module.translate(old),row['destination'])
            self.assertEqual(module.translate(row['destination']),row['destination'])

    def test_environment_text_preserves_unrelated_values(self):
        text='PHOTOBENCH_RESULTS_ROOT=/home/realmagic/xy/experiments/atm1013-20261118/cloud-results\nMODEL_NAME=unchanged\n'
        self.assertEqual(module.translate(text),'PHOTOBENCH_RESULTS_ROOT=/home/realmagic/xy/benchmarks/atm-bench/results-cloud\nMODEL_NAME=unchanged\n')

if __name__=='__main__':unittest.main()
