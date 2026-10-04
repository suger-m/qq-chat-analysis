"""Offline regression tests; fixtures contain invented identifiers only."""
import argparse
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/runtime'))
import qqlib
import qreport
import qvalidate
import cli
import qprivacy
import qpaths


class RuntimeTests(unittest.TestCase):
    def test_avatar_is_embedded_without_exposing_identifiers(self):
        ident, group = '112233445', '998877665'
        d = {'group': '测试群', 'gid': group,
             'headline': r'账号 112233445，文件 C:\Users\Example\Documents\private.txt',
             'people': [{'name': '成员甲', 'uin': ident, 'role': '分享者'}],
             'topics': [{'title': '讨论', 'contributors': [ident]}]}
        with patch('qqlib.QQStore') as store, patch('qqlib.avatar_data_uri', return_value='data:image/png;base64,AAAA'):
            store.return_value.nick_index.return_value = {'成员甲': ident}
            doc = qreport.render(d)
        self.assertIn('data:image/png;base64,AAAA', doc)
        self.assertIn('成员甲', doc)
        self.assertNotIn(ident, doc)
        self.assertNotIn(group, doc)
        self.assertNotIn(r'C:\Users\Example', doc)

    def test_context_counts_distinct_speakers(self):
        msgs = [{'uin': '112233445', 'ts': 1000}, {'uin': '112233445', 'ts': 1001},
                {'uin': '998877665', 'ts': 1002}, {'uin': '0', 'ts': 1003, 'system': True}]
        args = argparse.Namespace(group='测试群', date=None, days=None, from_date=None, to_date=None,
                                  limit=4, gap=30, json=True, max_chars=1000)
        with patch('cli.get_store'), patch('cli.resolve_group', return_value=('555555555', '测试群')), \
             patch('qanalysis.collect', return_value=msgs), patch('qanalysis.group_member_names', return_value={}), \
             patch('qanalysis.context_json', return_value=[]), patch('cli.out_json') as output:
            cli.cmd_context(args)
        self.assertEqual(output.call_args.args[0]['speakers'], 2)
        self.assertEqual(sum(output.call_args.args[0]['hourly']), 4)

    def test_missing_snapshot_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(qqlib, 'OUTPUT_DIR', str(Path(tmp) / 'absent')):
            self.assertIsNone(qqlib.detect_account())

    def test_short_verbatim_checks_author(self):
        d = {'quotes': [{'who': '成员甲', 'uin': '112233445', 'text': '你好'}]}
        report = qvalidate.Report()
        qvalidate._check_verbatim(d, report, {'你好': {'998877665'}}, set(), '你好')
        self.assertTrue(report.warnings)

    def test_context_indexes_short_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'context.txt'
            path.write_text('# 成员甲=112233445\n20:00 成员甲: 你好\n', encoding='utf-8')
            ctx = qvalidate.load_context(path)
        self.assertEqual(ctx['index']['你好'], {'112233445'})

    def test_store_validation_preserves_duplicate_nicknames(self):
        with patch('qanalysis.collect', return_value=[]), patch('qanalysis.group_member_names', return_value={'112233445': '同名', '998877665': '同名'}):
            ctx = qvalidate.load_from_store(Mock(), '555555555')
        self.assertEqual(set(ctx['dups']['同名']), {'112233445', '998877665'})

    def test_diagnostic_masks_account(self):
        report = qvalidate.Report()
        report.fix('people[0].uin', '补上 112233445')
        self.assertNotIn('112233445', report.text())

    def test_schema_uses_placeholders(self):
        self.assertNotRegex(qreport.SCHEMA, r'=\d{5,}')
        self.assertNotIn('100% 配上真实头像', qreport.SCHEMA)

    def test_contact_and_secret_redaction_preserves_topic_numbers(self):
        text = qprivacy.redact_text('联系 example@example.test，13800138000，api_key=sk-examplekey000000000。额度 1000000000 token')
        self.assertNotIn('example@example.test', text)
        self.assertNotIn('13800138000', text)
        self.assertNotIn('sk-examplekey', text)
        self.assertIn('1000000000 token', text)

    def test_account_paths_cannot_escape_output_directory(self):
        with self.assertRaises(ValueError):
            qpaths.account_id('../../private')
        self.assertEqual(qpaths.account_id('112233445'), '112233445')

    def test_private_data_cannot_be_written_inside_skill(self):
        with patch.dict('os.environ', {'QQ_DATA_DIR': str(ROOT / 'data')}):
            with self.assertRaises(ValueError):
                qpaths.data_dir()


if __name__ == '__main__':
    unittest.main()
