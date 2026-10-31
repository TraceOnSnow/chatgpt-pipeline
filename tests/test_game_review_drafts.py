import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from chatgpt_pipeline import bulk, game_reviews as games, notes
from chatgpt_pipeline.verify import encoded, sha


class GameReviewDraftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        module = patch.object(bulk, '__file__', str(self.base / 'src/chatgpt_pipeline/bulk.py'))
        module.start()
        self.addCleanup(module.stop)
        self.workspace = self.base / 'runtime/workbench'
        self.workspace.mkdir(parents=True)
        (self.workspace / 'jobs').mkdir()
        bulk.save(self.workspace / 'config.json', notes.config())
        with sqlite3.connect(self.workspace / 'state.sqlite3') as db:
            db.executescript('''CREATE TABLE calls(job_id TEXT,stage TEXT,request_hash TEXT,reservation TEXT,
                cost TEXT,response_id TEXT,started_at TEXT,PRIMARY KEY(job_id,stage));
                CREATE TABLE items(id TEXT PRIMARY KEY); CREATE TABLE jobs(id TEXT PRIMARY KEY);
                INSERT INTO items VALUES('historical-item'); INSERT INTO jobs VALUES('historical-job');''')
        self.source = self.base / 'personal-opinion.txt'
        self.source.write_text('这是本人确认过的私人短感受。')
        self.item = {'target_id': 'work-123', 'work_id': '123', 'entry_id': 'notion-entry-1', 'title': 'A game',
                     'has_personal_opinion': True,
                     'evidence': [{'kind': 'work-private-note', 'source_id': 'work:123',
                                   'text': self.source.read_text(), 'sha256': sha(self.source.read_bytes()),
                                   'path': str(self.source)}]}
        self.manifest = self.base / 'manifest.json'
        self.output = self.base / 'runtime/game-review-batch'

    def prepare(self):
        bulk.save(self.manifest, {'version': 'game-review-manifest-v1', 'items': [self.item]})
        return games.prepare(self.manifest, self.output, self.workspace)

    def target(self):
        return bulk.read(self.output / 'batch.json')['targets'][0]

    def response(self, text):
        result = io.BytesIO(encoded({'id': 'mock-response', 'model': bulk.MODEL, 'usage': {'cost': 0.001},
                    'choices': [{'finish_reason': 'stop', 'message': {'content': text}}]}))
        result.status = 200
        return result

    def review(self, markdown='# A game\n\n保留原有明确意见。', verdict='ready'):
        return encoded({'markdown': markdown, 'verdict': verdict, 'reason': '符合提供的本人资料。'}).decode()

    def test_offline_prepare_has_honest_external_identity_and_preserves_ui_tables(self):
        with patch('urllib.request.OpenerDirector.open') as network:
            report = self.prepare()
        network.assert_not_called()
        self.assertEqual(report['targets'], 1)
        self.assertEqual(report['shared_workspace_usage']['calls'], 0)
        target = self.target()
        definition = bulk.read(Path(target['directory']) / 'external-definition.json')
        self.assertEqual(definition['identity']['source_type'], games.PROTOCOL)
        self.assertEqual(definition['identity']['evidence_sha256'], [self.item['evidence'][0]['sha256']])
        self.assertFalse((Path(target['directory']) / 'definition.json').exists())
        with sqlite3.connect(self.workspace / 'state.sqlite3') as db:
            self.assertEqual(db.execute('SELECT id FROM items').fetchall(), [('historical-item',)])
            self.assertEqual(db.execute('SELECT id FROM jobs').fetchall(), [('historical-job',)])
        with self.assertRaisesRegex(ValueError, 'explicit --call'):
            games.run(self.output)

    def test_two_stages_use_shared_transport_ledger_and_resume_never_resends(self):
        self.prepare()
        with patch.object(notes, 'credential', return_value='mock-key'), \
             patch('urllib.request.OpenerDirector.open', side_effect=[self.response('# A game\n\n原有个人判断。'),
                                                                     self.response(self.review())]) as network:
            report = games.run(self.output, call=True, workers=1)
            self.assertEqual(report['states'], {'done': 1})
            self.assertEqual(report['shared_workspace_usage']['actual_usd'], '0.002')
            games.run(self.output, call=True, workers=1)
            self.assertEqual(network.call_count, 2)

    def test_unknown_opinion_always_gets_provisional_marker_and_review_state(self):
        self.item['has_personal_opinion'] = False
        self.prepare()
        with patch.object(notes, 'credential', return_value='mock-key'), \
             patch('urllib.request.OpenerDirector.open', side_effect=[self.response('# A game\n\n等待补充实际感受。'),
                       self.response(self.review('# A game\n\n等待补充实际感受。', 'ready'))]):
            report = games.run(self.output, call=True, workers=1)
        self.assertEqual(report['states'], {'review': 1})
        result = bulk.read(Path(self.target()['directory']) / 'external-result.json')
        self.assertIn('暂拟观点待确认', result['markdown'])
        self.assertEqual(result['review']['status'], 'needs_review')
        self.assertFalse(result['published'])

    def test_existing_global_budget_blocks_calls_and_retains_prior_ledger(self):
        self.prepare()
        with sqlite3.connect(self.workspace / 'state.sqlite3') as db:
            db.execute('INSERT INTO calls(job_id,stage,request_hash,reservation,cost) VALUES(?,?,?,?,?)',
                       ('historical', 'draft', 'frozen', '2', '2'))
        with patch.object(notes, 'credential', return_value='mock-key'), patch('urllib.request.OpenerDirector.open') as network:
            report = games.run(self.output, call=True, workers=1)
        network.assert_not_called()
        self.assertEqual(report['states'], {'attention': 1})
        self.assertEqual(report['shared_workspace_usage']['actual_usd'], '2')
        self.assertEqual(report['shared_workspace_usage']['calls'], 1)

    def test_ambiguous_draft_attempt_is_never_resent_even_after_state_reset(self):
        self.prepare()
        with patch.object(notes, 'credential', return_value='mock-key'), \
             patch('urllib.request.OpenerDirector.open', side_effect=TimeoutError) as network:
            games.run(self.output, call=True, workers=1)
            directory = Path(self.target()['directory'])
            bulk.save(directory / 'external-state.json', {'state': 'prepared'})
            report = games.run(self.output, call=True, workers=1)
            self.assertEqual(network.call_count, 1)
        self.assertEqual(report['shared_workspace_usage']['unknown_cost_calls'], 1)
        self.assertEqual(report['states'], {'attention': 1})

    def test_review_timeout_preserves_complete_draft_without_resending(self):
        self.prepare()
        with patch.object(notes, 'credential', return_value='mock-key'), \
             patch('urllib.request.OpenerDirector.open', side_effect=[self.response('# A game\n\n保留已有明确意见。'), TimeoutError]) as network:
            report = games.run(self.output, call=True, workers=1)
            games.run(self.output, call=True, workers=1)
            self.assertEqual(network.call_count, 2)
        self.assertEqual(report['states'], {'review': 1})
        self.assertEqual(report['drafts'], 1)
        self.assertEqual(report['shared_workspace_usage']['unknown_cost_calls'], 1)

    def test_source_sha_and_duplicate_target_guards(self):
        self.item['evidence'][0]['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'SHA mismatch'):
            self.prepare()
        self.item['evidence'][0]['sha256'] = sha(self.source.read_bytes())
        self.prepare()
        self.source.write_text('later source edit')
        with patch('urllib.request.OpenerDirector.open') as network:
            report = games.run(self.output, call=True, workers=1)
        network.assert_not_called()
        self.assertEqual(report['states'], {'attention': 1})
        self.item['evidence'][0].update(text=self.source.read_text(), sha256=sha(self.source.read_bytes()))
        bulk.save(self.manifest, {'version': 'game-review-manifest-v1', 'items': [self.item]})
        with self.assertRaisesRegex(ValueError, 'different frozen review job'):
            games.prepare(self.manifest, self.base / 'runtime/second', self.workspace)


    def test_missing_vault_document_resolves_only_by_stable_registry_identity_and_sha(self):
        from copy import deepcopy
        identity = '57798928-eb5f-4033-a90e-cdb3fb84d6f2'
        vault = self.base / 'vault'
        destination = vault / 'new' / 'source.md'
        destination.parent.mkdir(parents=True)
        raw = ('---\nid: ' + identity + '\nkind: note\n---\n# Source\n\nUnchanged body.\n').encode()
        destination.write_bytes(raw)
        registry = vault / '.markdown-library/state.json'
        registry.parent.mkdir()
        bulk.save(registry, {'notes': {identity: {'path': 'new/source.md', 'sha256': sha(raw), 'deleted': False}}})
        evidence = self.item['evidence'][0]
        evidence.pop('path')
        evidence.update(source_id='vault:' + identity + ':p137', document_path=str(vault / 'old/source.md'),
                        document_sha256=sha(raw))
        before = deepcopy(self.item)
        proofs = []
        self.assertEqual(games.verify_item(self.item, proofs), before)
        self.assertEqual(self.item, before)
        self.assertEqual(proofs[0]['resolved_path'], str(destination))
        self.assertEqual(proofs[0]['document_sha256'], sha(raw))
        destination.write_bytes(raw + b'edited')
        with self.assertRaisesRegex(ValueError, 'SHA mismatch'):
            games.verify_item(self.item)
        destination.write_bytes(raw)
        evidence['source_id'] = 'unregistered-document'
        with self.assertRaisesRegex(ValueError, 'stable Vault identity'):
            games.verify_item(self.item)
        evidence['source_id'] = 'vault:' + identity + ':p137'
        wrong = raw.replace(identity.encode(), b'11111111-1111-1111-1111-111111111111')
        destination.write_bytes(wrong)
        evidence['document_sha256'] = sha(wrong)
        bulk.save(registry, {'notes': {identity: {'path': 'new/source.md', 'sha256': sha(wrong), 'deleted': False}}})
        with self.assertRaisesRegex(ValueError, 'identity differs'):
            games.verify_item(self.item)


if __name__ == '__main__':
    unittest.main()
