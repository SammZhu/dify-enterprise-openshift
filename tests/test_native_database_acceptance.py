"""Execute production database acceptance with real Ansible and shell parsing."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

import yaml
import test_native_workflows as native


class DatabaseAcceptance(unittest.TestCase):
    run_tasks = native.NativeTasks.run_tasks

    def exercise(self, missing_database=False, query_failure=False, success=True):
        with tempfile.TemporaryDirectory(prefix='native-db-check-') as td:
            root = Path(td).resolve()
            bindir = root / 'bin'
            bindir.mkdir()
            capture = root / 'psql-argv.json'
            # The API boundary dispatches the production argv into a real bash.
            # The psql boundary checks its actual arguments, not the source string.
            oc = bindir / 'oc'
            oc.write_text('#!' + sys.executable + '\n' + '''import os, subprocess, sys
args = sys.argv[1:]
assert args[:5] == ['exec', 'dify-postgresql-0', '-n', 'dify', '--']
env = dict(os.environ, POSTGRESQL_USER='fixture-user',
    POSTGRESQL_PASSWORD='fixture password with spaces $ and quotes')
sys.exit(subprocess.call(args[5:], env=env))
''')
            psql = bindir / 'psql'
            psql.write_text('#!' + sys.executable + '\n' + '''import json, os, sys
from pathlib import Path
Path(os.environ['DB_ARGV_CAPTURE']).write_text(json.dumps(sys.argv[1:]))
expected = ['-h', '127.0.0.1', '-U', 'fixture-user', '-d', 'postgres',
    '-Atc', 'SELECT datname FROM pg_database']
if sys.argv[1:] != expected:
    sys.stderr.write('SQL did not arrive as one argument')
    sys.exit(2)
assert os.environ['PGPASSWORD'] == 'fixture password with spaces $ and quotes'
if os.environ['DB_QUERY_FAILURE'] == '1':
    sys.stderr.write('private-fixture-db-error')
    sys.exit(3)
print('postgres\\ndify\\nenterprise\\naudit')
if os.environ['DB_MISSING_DATABASE'] != '1': print('dify_plugin_daemon')
''')
            oc.chmod(0o755)
            psql.chmod(0o755)
            tasks = yaml.safe_load((native.TASKS / 'install/check.yml').read_text())
            query = next(t for t in tasks if t['name'].startswith('Query PostgreSQL'))
            required = next(t for t in tasks if t['name'] == 'Require all four application databases')
            query['environment'] = {
                'PATH': str(bindir) + os.pathsep + os.environ['PATH'],
                'DB_ARGV_CAPTURE': str(capture),
                'DB_MISSING_DATABASE': str(int(missing_database)),
                'DB_QUERY_FAILURE': str(int(query_failure)),
            }
            result = self.run_tasks(root, [query, required], {'dify_oc': [str(oc)]}, success=success)
            self.assertEqual(json.loads(capture.read_text()), ['-h', '127.0.0.1', '-U',
                'fixture-user', '-d', 'postgres', '-Atc', 'SELECT datname FROM pg_database'])
            self.assertNotIn('private-fixture-db-error', result.stdout + result.stderr)
            self.assertNotIn('fixture password with spaces', result.stdout + result.stderr)

    def test_database_query_keeps_sql_as_one_argument_and_checks_four_databases(self):
        self.exercise()

    def test_missing_application_database_blocks_acceptance(self):
        self.exercise(missing_database=True, success=False)

    def test_query_error_is_not_published(self):
        self.exercise(query_failure=True, success=False)


if __name__ == '__main__':
    unittest.main()
