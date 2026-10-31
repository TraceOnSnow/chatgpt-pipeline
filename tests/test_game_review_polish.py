import io
import unittest
from unittest.mock import patch
from pathlib import Path

from chatgpt_pipeline import bulk, game_review_polish as polish, notes
from chatgpt_pipeline.verify import encoded, sha
import test_game_review_drafts as draft_tests


class GameReviewPolishTests(unittest.TestCase):
    def setUp(self):
        f = draft_tests.GameReviewDraftTests('test_offline_prepare_has_honest_external_identity_and_preserves_ui_tables')
        f.setUp()
        self.addCleanup(f.doCleanups)
        f.prepare()
        self.f = f
        target = f.target()
        self.directory = Path(target['directory'])
        text = '# A game\n\n暂拟观点待确认。旧稿等待改写。\n'
        bulk.atomic_bytes(self.directory / 'note.md', text.encode())
        bulk.save(self.directory / 'external-result.json', {'markdown': text, 'markdown_sha256': sha(text.encode())})
        bulk.save(self.directory / 'external-state.json', {'state': 'review'})
        self.output = f.base / 'runtime/polish'
        polish.prepare(f.output, self.output)

    def response(self, values):
        return self.f.response(encoded(values).decode())

    def test_ordered_batches_share_two_call_ledger_and_never_modify_original_jobs(self):
        before = {p.name: p.read_bytes() for p in self.directory.iterdir() if p.is_file()}
        values = ['# A game\n\n暂拟观点待确认。按我的口味，这篇我想从系统组合的自由度谈起。']
        with patch.object(notes, 'credential', return_value='mock-key'), \
             patch('urllib.request.OpenerDirector.open', side_effect=[self.response(values), self.response(values)]) as network:
            report = polish.run(self.output, call=True, workers=1)
            self.assertEqual(report['polished_drafts'], 1)
            self.assertEqual(report['shared_workspace_usage']['calls'], 2)
            polish.run(self.output, call=True, workers=1)
            self.assertEqual(network.call_count, 2)
        for name, value in before.items():
            self.assertEqual((self.directory / name).read_bytes(), value)
        result = bulk.read(self.output / 'drafts.json')['items'][0]
        self.assertFalse(result['published'])
        self.assertEqual(Path(result['note_path']).read_text(), values[0] + '\n')

    def test_wrong_titles_length_and_unplayed_inference_are_rejected(self):
        target = bulk.read(self.output / 'batch.json')['batches'][0]
        records = bulk.read(Path(target['directory']) / 'polish-input.json')['records']
        for values in ([], ['# Other game\n\n正文。'], ['# A game\n\n得玩了才敢说。'], ['# A game\n\n资料显示需要补充。']):
            with self.assertRaises(ValueError):
                polish.parse(encoded(values).decode(), records)
        with self.assertRaisesRegex(ValueError, 'explicit --call'):
            polish.run(self.output)

    def test_source_sha_mutation_blocks_all_calls(self):
        self.directory.joinpath('note.md').write_text('# A game\n\n用户后来修改。')
        with patch('urllib.request.OpenerDirector.open') as network:
            report = polish.run(self.output, call=True, workers=1)
        network.assert_not_called()
        self.assertEqual(report['states'], {'attention': 1})

    def test_review_timeout_retains_polished_draft_and_reservation_without_resend(self):
        values = ['# A game\n\n暂拟观点待确认。按我的口味，想讨论规则如何改变取舍。']
        with patch.object(notes, 'credential', return_value='mock-key'), \
             patch('urllib.request.OpenerDirector.open', side_effect=[self.response(values), TimeoutError]) as network:
            report = polish.run(self.output, call=True, workers=1)
            polish.run(self.output, call=True, workers=1)
            self.assertEqual(network.call_count, 2)
        self.assertEqual(report['polished_drafts'], 1)
        self.assertEqual(report['shared_workspace_usage']['unknown_cost_calls'], 1)

    def test_unknown_attempt_cannot_be_resent_even_after_state_reset(self):
        with patch.object(notes, 'credential', return_value='mock-key'), \
             patch('urllib.request.OpenerDirector.open', side_effect=TimeoutError) as network:
            polish.run(self.output, call=True, workers=1)
            target = bulk.read(self.output / 'batch.json')['batches'][0]
            bulk.save(Path(target['directory']) / 'polish-state.json', {'state': 'prepared'})
            polish.run(self.output, call=True, workers=1)
            self.assertEqual(network.call_count, 1)


    def test_algorithm_homonym_is_excluded_only_from_model_view_with_auditable_source_id(self):
        from copy import deepcopy
        item = deepcopy(self.f.item)
        source = {'kind': 'current_private_note', 'source_id': 'vault:stable-id:p137',
                  'text': '来源：我的秋招 C++17 做题模板；原章节：### Kruskal。\n最小生成森林不是游戏评价。',
                  'sha256': 'frozen-excerpt-hash'}
        item['evidence'].append(source)
        before = deepcopy(item)
        excluded = polish.excluded_sources(item)
        self.assertEqual(excluded[0]['source_id'], source['source_id'])
        view = polish.model_item({'title': item['title'], 'item': item, 'excluded_sources': excluded})
        self.assertNotIn(source, view['evidence'])
        self.assertEqual(item, before)
        self.assertIn(self.f.item['evidence'][0], view['evidence'])


    def test_offline_fenced_draft_recovery_preserves_history_and_has_manual_status(self):
        values = ['# A game\n\n暂拟观点待确认。按我的口味，想讨论规则组合。']
        response = self.f.response('```json\n' + encoded(values).decode() + '\n```')
        with patch.object(notes, 'credential', return_value='mock-key'), \
             patch('urllib.request.OpenerDirector.open', return_value=response):
            polish.run(self.output, call=True, workers=1)
        target = bulk.read(self.output / 'batch.json')['batches'][0]
        directory = Path(target['directory'])
        original_state = (directory / 'polish-state.json').read_bytes()
        original_raw = (directory / 'draft/response.raw').read_bytes()
        with patch('urllib.request.OpenerDirector.open') as network:
            recovery = polish.recover_offline(self.output)
            exported = polish.export(self.output)
        network.assert_not_called()
        self.assertEqual(recovery['packaging'], {'unwrap_complete_json_fence': 1})
        self.assertEqual(recovery['recovered_draft_articles'], 1)
        self.assertEqual(exported['polished_drafts'], 1)
        self.assertEqual((directory / 'polish-state.json').read_bytes(), original_state)
        self.assertEqual((directory / 'draft/response.raw').read_bytes(), original_raw)
        self.assertEqual(bulk.read(directory / 'polish-recovered-result.json')['item_recovery'][0]['status'], 'manual_pending')

    def test_offline_complete_review_is_preferred_and_only_generated_exports_are_updated(self):
        draft = ['# A game\n\n暂拟观点待确认。按我的口味，想讨论规则组合。']
        review = ['# A game\n\n暂拟观点待确认。这篇我想从规则产生的取舍谈起。']
        with patch.object(notes, 'credential', return_value='mock-key'), \
             patch('urllib.request.OpenerDirector.open', side_effect=[self.response(draft),
                 self.f.response('```json\n' + encoded(review).decode() + '\n```')]):
            polish.run(self.output, call=True, workers=1)
        recovery = polish.recover_offline(self.output)
        self.assertEqual(recovery['recovered_review_articles'], 1)
        polish.export(self.output)
        record = bulk.read(self.output / 'drafts.json')['items'][0]
        self.assertEqual(Path(record['note_path']).read_text(), review[0] + '\n')
        self.assertEqual(record['review']['source_stage'], 'review')
        target = bulk.read(self.output / 'batch.json')['batches'][0]
        records = bulk.read(Path(target['directory']) / 'polish-input.json')['records']
        normalized, changes = polish.recover_array(encoded(['# 《A game》\n\n正文。']).decode(), records)
        self.assertIn('normalize_equivalent_title', changes)
        self.assertEqual(normalized[0].splitlines()[0], '# A game')
        untranslated, _ = polish.recover_array(encoded(['# 另一款游戏\n\n正文。']).decode(), records)
        with self.assertRaisesRegex(ValueError, 'title/order'):
            polish.parse(encoded(untranslated).decode(), records)


if __name__ == '__main__':
    unittest.main()
