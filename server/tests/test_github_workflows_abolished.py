
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIR = REPOSITORY_ROOT / '.github' / 'workflows'


def test_publish_release_and_docker_workflows_are_abolished() -> None:
    """
    廃止方針: publish_release / publish_docker_image は復元せず不在を受け入れる。

    KTV-AUD-053 / 054 は欠陥のある workflow を直す代わりに、
    廃止済み（ファイル不在）を受け入れ条件として固定する。
    """

    assert WORKFLOWS_DIR.exists() is False or (
        (WORKFLOWS_DIR / 'publish_release.yaml').exists() is False and
        (WORKFLOWS_DIR / 'publish_release.yml').exists() is False and
        (WORKFLOWS_DIR / 'publish_docker_image.yaml').exists() is False and
        (WORKFLOWS_DIR / 'publish_docker_image.yml').exists() is False
    )


def test_no_ghcr_prune_targets_unrelated_konomitv_package() -> None:
    """
    残存 workflow があっても、別 package 名 konomitv を prune 対象にしないこと。
    """

    if WORKFLOWS_DIR.exists() is False:
        return
    for workflow in WORKFLOWS_DIR.glob('*.y*ml'):
        text = workflow.read_text(encoding='utf-8')
        # 旧バグ: prune container: konomitv が build 先 KonomiTV-BS4K と不一致
        assert 'container: konomitv' not in text
        assert "container: 'konomitv'" not in text
        assert 'container: "konomitv"' not in text
