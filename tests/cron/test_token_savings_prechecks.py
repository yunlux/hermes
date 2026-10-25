"""Wake gates must avoid inference on unchanged/bad input and retain unhandled changes."""
from datetime import datetime
import json
import pytest
from cron.token_savings_prechecks import observe, commit_success


@pytest.fixture(autouse=True)
def private_precheck_settings(tmp_path):
    directory = tmp_path / "home/cron/prechecks"
    directory.mkdir(parents=True)
    (directory / "config.json").write_text(json.dumps({"jobs": {
        "test-vault-job": "vault", "test-github-job": "github"}}))


def test_vault_missing_root_unchanged_and_quota_skip(tmp_path):
    home=tmp_path/'home';home.mkdir(exist_ok=True);vault=tmp_path/'vault'
    assert observe(home,'vault',vault=vault)['wakeAgent'] is False
    daily=vault/'daily';daily.mkdir(parents=True);(daily/'今日状态.md').write_text('real input')
    now=datetime(2026,10,8)
    assert observe(home,'vault',vault=vault,now=now)['wakeAgent'] is True
    # A free quota skip does not commit the change; next fire still processes it.
    assert observe(home,'vault',vault=vault,now=now)['wakeAgent'] is True
    commit_success(home,{'id':'test-vault-job'})
    assert observe(home,'vault',vault=vault,now=now)['wakeAgent'] is False
    (daily/'今日状态.md').write_text('new real input')
    assert observe(home,'vault',vault=vault,now=now)['wakeAgent'] is True


def test_github_all_platform_digest_no_model(tmp_path):
    home=tmp_path/'home';home.mkdir(exist_ok=True);calls=[]
    def fetch(url):
        calls.append(url)
        if 'github.com' in url:return json.dumps({'items':[{'id':1,'pushed_at':'fixed','updated_at':'fixed'}]}).encode()
        if 'algolia' in url:return json.dumps({'hits':[{'objectID':'s','points':30,'num_comments':10}]}).encode()
        if 'reddit' in url:return json.dumps({'data':{'children':[{'data':{'id':'r','score':5,'num_comments':2}}]}}).encode()
        return b'fixed feed'
    assert observe(home,'github',fetch=fetch)['wakeAgent'] is True
    commit_success(home,{'id':'test-github-job'})
    assert observe(home,'github',fetch=fetch)['wakeAgent'] is False
    assert any('arxiv' in u for u in calls) and any('huggingface' in u for u in calls)
    def offline(url):raise OSError('source offline')
    result=observe(home,'github',fetch=offline)
    assert result['wakeAgent'] is False and result['precheckError'] is True
