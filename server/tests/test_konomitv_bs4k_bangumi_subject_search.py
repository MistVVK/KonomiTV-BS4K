from app.metadata.KonomiTVBS4KBangumiSubjectSearch import (
    BANGUMI_SUBJECT_SEARCH_RULES,
    BuildBangumiSubjectSearchDescription,
    ChooseSubjectFromHints,
)


def test_fabricated_subject_id_outside_hints_is_discarded() -> None:
    """hints に無い ID は捨て、未紐付けのままにする。"""

    subjects = [
        {'id': 10, 'type': 2, 'name': 'テスト作品', 'name_cn': ''},
    ]
    assert ChooseSubjectFromHints('テスト作品', subjects, proposed_subject_id=999) is None


def test_ambiguous_search_hints_stay_unlinked() -> None:
    """同点の検索候補が複数ある場合は曖昧として未紐付けにする。"""

    subjects = [
        {'id': 1, 'type': 2, 'name': '同名作品', 'name_cn': ''},
        {'id': 2, 'type': 2, 'name': '同名作品', 'name_cn': ''},
    ]
    assert ChooseSubjectFromHints('同名作品', subjects) is None


def test_generic_title_is_not_linked() -> None:
    """汎用枠のタイトルは検索 hints があっても条目へ結び付けない。"""

    subjects = [
        {'id': 3, 'type': 2, 'name': 'ニュース', 'name_cn': ''},
    ]
    assert ChooseSubjectFromHints('ニュース', subjects) is None


def test_nonempty_series_description_still_includes_bangumi_rules() -> None:
    """既存の番組説明があっても Bangumi 専用規則を先頭に残す。"""

    description = BuildBangumiSubjectSearchDescription('既存の番組概要')
    assert description.startswith(BANGUMI_SUBJECT_SEARCH_RULES)
    assert '既存の番組概要' in description
    assert 'choice_id' in description
    assert 'unresolved' in description
