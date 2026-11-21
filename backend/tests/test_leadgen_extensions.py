"""Offline extension contract, including Python -> actual Node authorization."""
import base64
import json
import sqlite3
from pathlib import Path
import subprocess

import pytest

from test_leadgen_config_contract import isolated, stored_actions, VIDEO
from app.execution import leadgen_config as cfg, live_workflow as flow, live_leadgen, live_scope
from app.execution.leadgen_media import EXTRA_FIELDS, image_info, mentions_confirmed, verify
from app.runtime import browser, store
from app.api import task_runner
from app import leadgen_pools
from tools.prepare_live_workflow_scope import prepare

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a4E8AAAAASUVORK5CYII=')


@pytest.fixture
def extension(isolated, monkeypatch):
    db, payload, calls, comments = isolated
    original = browser.execute
    profiles = {c['userId']: {'userId': c['userId'], 'nickname': c['nickname'], 'private': False, 'gender': 'male', 'age': 28} for c in comments}
    def read(command, **kw):
        if command['type'] == 'inspect-profile':
            calls.append(command)
            return {'ok': True, 'item': profiles.get(command['recipientId'], {})}
        if command['type'] == 'inspect-share-targets':
            calls.append(command)
            return {'ok': True, 'receivers': [dict(name=name, id='group_123456', kind='group') for name in command['receiverNames'] if name != '不存在的人'][:1]}
        return original(command, **kw)
    monkeypatch.setattr(browser, 'execute', read)
    return db, {**payload, 'enableVideoComment': False, 'enableFollow': True, 'followLimit': 3,
                'targetGender': 'male', 'ageFilterEnabled': True, 'targetAgeMin': 20, 'targetAgeMax': 35,
                'followDmDelayMin': 0, 'followDmDelayMax': 0}, calls, comments, profiles


def test_follow_only_reads_real_profiles_and_deduplicates_across_videos(extension):
    db, payload, calls, _, _ = extension
    selected = flow.preview(db, payload)
    assert [a['type'] for a in selected['actions']] == ['follow-user'] * 3
    assert len({a['recipientId'] for a in selected['actions']}) == 3
    assert all(c['type'] in {'search-videos', 'read-comments', 'inspect-profile'} for c in calls)


def test_profile_filters_skip_hidden_changed_private_and_wrong_gender(extension):
    db, payload, _, comments, profiles = extension
    profiles[comments[0]['userId']]['gender'] = None
    profiles[comments[1]['userId']]['age'] = None
    profiles[comments[2]['userId']]['private'] = True
    profiles[comments[3]['userId']]['nickname'] = '换了作者'
    selected = flow.preview(db, payload)
    assert {a['recipientId'] for a in selected['actions']} == {comments[4]['userId']}
    assert len(selected['skippedActions']) >= 4
    assert cfg.profile_reason({**payload, 'targetGender': 'male_unknown'}, {'private': False, 'gender': None, 'age': 30}) == ''
    assert cfg.profile_reason(payload, {'private': False, 'gender': 'female', 'age': 30})


def test_shares_rotate_exact_names_keep_spaces_and_obey_per_video_and_account_caps(extension):
    db, payload, calls, _, _ = extension
    payload.update(enableFollow=False, enableProfileShare=True, profileShareFriends='不存在的人\n工作流 交流群\n测试群二',
                   profileShareLimitEnabled=True, profileShareLimit=2, enableCommentShare=True,
                   commentShareFriends='评论 测试群', commentShareMin=1, commentShareMax=1)
    selected = flow.preview(db, payload)
    assert len(selected['actions']) == 4
    profiles = [a for a in selected['actions'] if a['type'] == 'share-profile']
    comments = [a for a in selected['actions'] if a['type'] == 'share-comment']
    assert [a['shareRecipient'] for a in profiles] == ['工作流 交流群', '测试群二']
    assert len({a['videoUrl'] for a in comments}) == 2
    assert all(a['shareRecipientId'] == 'group_123456' and a['shareRecipientKind'] == 'group' for a in selected['actions'])
    stored = [a for a in stored_actions(db, selected['proposalId']) if a['type'] == 'share-comment']
    assert all(a['sourceText'] and a['commentId'] for a in stored)
    assert all('commentId' not in a for a in comments)
    assert all(c['type'].startswith(('search', 'read', 'inspect')) for c in calls)


def test_extension_total_budget_and_exact_python_node_signature(extension, tmp_path, monkeypatch):
    db, payload, _, _, _ = extension
    payload.update(enableProfileShare=True, profileShareFriends='测试群', enableCommentShare=True, commentShareFriends='测试好友',
                   enableVideoComment=True, enableVideoCommentImage=True, videoCommentAttachmentPercent=100,
                   interactionLimit=6)
    photo = tmp_path / '测试图片.png'
    photo.write_bytes(PNG)
    payload['videoCommentImagePaths'] = [str(photo)]
    selected = flow.preview(db, payload)
    assert len(selected['actions']) == 6
    file = tmp_path / 'approved.json'
    prepare(db, selected['proposalId'], selected['binding'], file)
    monkeypatch.setenv('LEADS_LEADGEN_SCOPE', str(file))
    commands = []
    for a in stored_actions(db, selected['proposalId']):
        cmd = live_scope.signed_command(a['type'], account_id='a', task_id='task', video_url=a['videoUrl'],
            text=a.get('text', ''), comment_id=a.get('commentId', ''), comment_author=a.get('commentAuthor', ''),
            recipient_id=a.get('recipientId', ''), extras={k: a[k] for k in EXTRA_FIELDS if k in a})
        assert live_scope.command_allowed(cmd)
        commands.append(cmd)
        for field in ('shareRecipient', 'shareRecipientId', 'attachmentPath', 'attachmentSha256'):
            assert not live_scope.command_allowed({**cmd, field: 'tampered'})
    script = "const p=JSON.parse(require('fs').readFileSync(0,'utf8'));const {scopeFor}=require('./desktop/live-platform');process.stdout.write(JSON.stringify(p.commands.map(c=>!!scopeFor(c,p.file))));"
    result = subprocess.run(['node', '-e', script], input=json.dumps({'file': str(file), 'commands': commands}),
        text=True, encoding='utf8', capture_output=True, check=True, cwd=Path(__file__).resolve().parents[2], timeout=15)
    assert json.loads(result.stdout) == [True] * len(commands)
    # A selected picture cannot silently turn into a different local file.
    photo.write_bytes(PNG + b'changed')
    with pytest.raises(ValueError, match='附图发生改变'):
        flow.read_proposal(db, selected['proposalId'], selected['binding'])
    sent = []
    monkeypatch.setattr(browser, 'execute', lambda *a, **kw: sent.append(a))
    action = next(a for a in selected['actions'] if a.get('attachmentPath'))
    result = live_leadgen.perform(db, action['type'], account_id='a', task_id='task', video_url=action['videoUrl'],
        text=action['text'], extras={k: action[k] for k in EXTRA_FIELDS if k in action})
    assert result['status'] == 'blocked' and not sent


@pytest.mark.parametrize('change', [
    {'profileShareFriends': ''}, {'profileShareFriends': ['群聊']}, {'profileShareSaveQr': True, 'profileQrRunKey': '../escape'},
    {'followLimitMin': 4, 'followLimitMax': 3}, {'targetGender': 'guess'},
    {'enableVideoComment': True, 'enableVideoCommentImage': True, 'videoCommentImagePaths': []},
    {'commentAttachmentPercent': 101}, {'followDmDelayMax': 3601},
])
def test_invalid_extension_settings_never_touch_a_browser(extension, change):
    db, payload, calls, _, _ = extension
    with pytest.raises(ValueError):
        flow.preview(db, {**payload, 'enableProfileShare': True, 'profileShareFriends': '测试群', **change})
    assert calls == []


def test_non_image_and_deleted_media_fail_before_search(isolated, tmp_path):
    db, payload, calls, _ = isolated
    file = tmp_path / 'fake.png'
    file.write_bytes(b'not a real image at all')
    for candidate in (file, tmp_path / 'missing.jpg'):
        with pytest.raises(ValueError):
            flow.preview(db, {**payload, 'enableVideoCommentImage': True, 'videoCommentImagePaths': [str(candidate)]})
    assert not calls


def test_native_gif_mentions_and_qr_settings_are_validated_and_bound(isolated):
    _, payload, _, _ = isolated
    config = cfg.normalize({**payload, "enableVideoCommentExpression": True,
        "videoCommentExpressionCount": 3, "enableVideoCommentMention": True,
        "videoCommentMentionNicknames": "AI助手，工作流顾问", "videoCommentMentionPercent": 80,
        "videoCommentMentionPosition": "after", "enableVideoComment": True})
    assert config["_videoCommentMentions"] == ["AI助手", "工作流顾问"]
    assert config["videoCommentMentionPosition"] == "after"
    for change in ({"videoCommentExpressionCount": 9}, {"videoCommentMentionPercent": 101},
                   {"videoCommentMentionPosition": "middle"}, {"videoCommentMentionNicknames": ""}):
        with pytest.raises(ValueError):
            cfg.normalize({**payload, "enableVideoComment": True, "enableVideoCommentMention": True,
                           "videoCommentMentionNicknames": "AI助手", **change})
    action = {"type": "post-comment", "text": "@AI助手 你好", "commentBody": "你好",
        "mentionPosition": "before", "mentionTargets": json.dumps([
            {"query": "AI助手", "id": "user_123456", "name": "AI助手"}], ensure_ascii=False),
        "stickerId": "gif_123456", "stickerName": "点头", "stickerUrl": "https://cdn.example/gif.webp"}
    verify(action)
    assert mentions_confirmed([{ "type": 0, "user_id": "user_123456", "start": 0, "end": 5 }],
                              json.loads(action["mentionTargets"]), action["text"])
    assert not mentions_confirmed([{ "type": 0, "user_id": "wrong_123456", "start": 0, "end": 5 }],
                                   json.loads(action["mentionTargets"]), action["text"])
    with pytest.raises(ValueError, match="账号信息无效"):
        verify({**action, "text": "被篡改"})
    with pytest.raises(ValueError, match="二维码"):
        verify({"type": "share-profile", "saveProfileQr": "1", "profileQrRunKey": "../bad"})


@pytest.mark.parametrize('mention_position,resolved', [('before', 'before'), ('after', 'after'),
                                                      ('random', 'before'), ('random', 'after')])
def test_gif_and_mention_targets_flow_into_exact_review_actions(isolated, monkeypatch, mention_position, resolved):
    db, payload, calls, _ = isolated
    original = browser.execute
    def read(command, **kwargs):
        if command["type"] == "inspect-comment-extras":
            calls.append(command)
            return {"ok": True,
                "stickers": [{"id": f"gif_{i:06}", "name": f"GIF{i}", "url": f"https://cdn.example/{i}.webp"} for i in range(3)],
                "mentions": [{"query": name, "id": f"user_{i:06}", "name": name}
                             for i, name in enumerate(command["mentionQueries"])]}
        return original(command, **kwargs)
    monkeypatch.setattr(browser, "execute", read)
    from app.execution import leadgen_plan
    choice = leadgen_plan.random.choice
    monkeypatch.setattr(leadgen_plan.random, 'choice', lambda values: resolved if values == ('before', 'after') else choice(values))
    payload.update(videoMin=1, videoMax=1, interactionLimit=4, enableVideoComment=True,
        enableVideoCommentExpression=True, videoCommentExpressionCount=2, videoCommentAttachmentPercent=100,
        enableVideoCommentMention=True, videoCommentMentionNicknames="AI助手", videoCommentMentionPercent=100,
        enableComment=True, enableCommentExpression=True, commentExpressionCount=3, commentAttachmentPercent=100,
        enableCommentMention=True, commentMentionNicknames="工作流顾问", commentMentionPercent=100,
        videoCommentMentionPosition=mention_position, commentMentionPosition=mention_position)
    result = flow.preview(db, payload)
    actions = result["actions"]
    assert [a["type"] for a in actions] == ["post-comment", "reply-comment"]
    assert actions[0]["stickerId"] == "gif_000000" and actions[1]["stickerId"] == "gif_000000"
    assert json.loads(actions[0]["mentionTargets"])[0] == {"query": "AI助手", "id": "user_000000", "name": "AI助手"}
    assert json.loads(actions[1]["mentionTargets"])[0] == {"query": "工作流顾问", "id": "user_000000", "name": "工作流顾问"}
    assert all(a['mentionPosition'] == resolved for a in actions)
    assert actions[0]["text"] == ('@AI助手 ' + actions[0]['commentBody'] if resolved == 'before' else actions[0]['commentBody'] + ' @AI助手')
    assert actions[1]["text"] == ('@工作流顾问 ' + actions[1]['commentBody'] if resolved == 'before' else actions[1]['commentBody'] + ' @工作流顾问')
    for action in actions:
        verify(action)
    assert len([c for c in calls if c["type"] == "inspect-comment-extras"]) == 2


def test_new_action_success_and_failure_counters_are_persisted_separately(extension, tmp_path, monkeypatch):
    db, payload, _, _, _ = extension
    payload.update(enableProfileShare=True, profileShareFriends='测试群', enableCommentShare=True, commentShareFriends='测试群')
    result = flow.preview(db, payload)
    file = tmp_path / 'approved.json'
    prepare(db, result['proposalId'], result['binding'], file)
    monkeypatch.setenv('LEADS_LEADGEN_SCOPE', str(file))
    scope = live_scope.load_scope()
    monkeypatch.setattr(browser, 'execute', lambda c, **kw: {'ok': True, 'items': [{'url': a['videoUrl']} for a in result['actions']]})
    attempted = []
    monkeypatch.setattr(live_leadgen, 'perform', lambda db, kind, **kw: attempted.append(kind) or {'ok': True, 'status': 'sent_ok'})
    ctx = task_runner.RunCtx(task_id='run', leadgen_id='task', account_id='a', db_path=db, payload=payload)
    ctx.run_id = store.run_begin(db, 'leadgen', 'run', '离线扩展测试', 'a', trigger='manual', engine='fixture')
    flow.run_reviewed(ctx, scope)
    assert ctx.stats['follows'] == attempted.count('follow-user') == 3
    assert ctx.stats['profileShares'] == attempted.count('share-profile')
    assert ctx.stats['commentShares'] == attempted.count('share-comment')
    assert leadgen_pools.load_records(db)['task']['stats']['follows'] == 3


def test_follow_dedup_is_user_based_share_comment_is_comment_based():
    user = {'type': 'follow-user', 'videoUrl': VIDEO, 'recipientId': 'user_123456', 'commentId': 'c1'}
    assert flow.action_key('a', user) == flow.action_key('a', {**user, 'videoUrl': VIDEO + '9', 'commentId': 'c2'})
    assert flow.action_key('a', {**user, 'type': 'share-comment'}) != flow.action_key('a', {**user, 'type': 'share-comment', 'commentId': 'c2'})


@pytest.mark.parametrize('status', ['uncertain', 'sent_fail'])
def test_failed_extension_stops_without_retry_or_success_count(extension, tmp_path, monkeypatch, status):
    db, payload, _, _, _ = extension
    result = flow.preview(db, payload)
    file = tmp_path / 'scope.json'
    prepare(db, result['proposalId'], result['binding'], file)
    monkeypatch.setenv('LEADS_LEADGEN_SCOPE', str(file))
    monkeypatch.setattr(browser, 'execute', lambda c, **kw: {'ok': True, 'items': [{'url': a['videoUrl']} for a in result['actions']]})
    attempts = []
    monkeypatch.setattr(live_leadgen, 'perform', lambda db, kind, **kw: attempts.append(kind) or {'ok': False, 'status': status})
    ctx = task_runner.RunCtx(task_id='run', leadgen_id='task', account_id='a', db_path=db, payload=payload)
    flow.run_reviewed(ctx, live_scope.load_scope())
    assert attempts == ['follow-user']
    assert ctx.stats.get('follows', 0) == 0
    assert ctx.stats['uncertain' if status == 'uncertain' else 'failed'] == 1


def test_photo_readback_cannot_confirm_a_text_only_comment(isolated, tmp_path, monkeypatch):
    db, payload, _, _ = isolated
    file = tmp_path / 'photo.png'
    file.write_bytes(PNG)
    selected = flow.preview(db, {**payload, 'enableVideoCommentImage': True, 'videoCommentImagePaths': [str(file)], 'videoCommentAttachmentPercent': 100})
    scope_file = tmp_path / 'scope.json'
    prepare(db, selected['proposalId'], selected['binding'], scope_file)
    monkeypatch.setenv('LEADS_LEADGEN_SCOPE', str(scope_file))
    action = selected['actions'][0]
    monkeypatch.setattr(browser, 'execute', lambda *a, **kw: {'ok': False, 'status': 'uncertain', 'imageUri': 'tos-cn/reviewed_image'})
    result = live_leadgen.perform(db, 'post-comment', account_id='a', task_id='task', video_url=action['videoUrl'],
        text=action['text'], extras={k: action[k] for k in EXTRA_FIELDS if k in action}, run_id='run')
    assert result['status'] == 'uncertain'
    with sqlite3.connect(db) as con:
        row_id = con.execute("SELECT id FROM live_action_results WHERE action='post-comment'").fetchone()[0]
        details = json.loads(con.execute('SELECT body FROM live_action_details WHERE result_id=?', (row_id,)).fetchone()[0])
    assert details['attachmentSha256'] == action['attachmentSha256']
    confirmation = {'http': 200, 'platform_code': 0, 'video_id': action['videoUrl'].split('/')[-1],
        'text': action['text'], 'nickname': '测试甲', 'comment_id': '987654321'}
    assert not live_leadgen.reconcile_readback(db, row_id, confirmation)
    assert not live_leadgen.reconcile_readback(db, row_id, {**confirmation, 'image_uri': 'different_image'})
    assert live_leadgen.reconcile_readback(db, row_id, {**confirmation, 'image_uri': 'tos-cn/reviewed_image'})
