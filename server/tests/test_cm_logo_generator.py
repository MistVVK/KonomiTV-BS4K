import asyncio

from app.metadata.CMLogoGenerator import (
    UnavailableCMLogoGenerator,
)


def test_automatic_logo_generation_capability_is_stably_unavailable() -> None:
    """現行リリースは自動作成を実装済みと誤表示しない。"""

    generator = UnavailableCMLogoGenerator()

    assert generator.capability.availability == 'Unavailable'
    assert generator.capability.reason_code == 'NotImplemented'


def test_unavailable_generator_never_produces_an_execution_request() -> None:
    """Unavailable実装は外部実行に必要なパスや終了コードを生成しない。"""

    generator = UnavailableCMLogoGenerator()
    result = asyncio.run(generator.generate(recorded_file_path='ignored'))

    assert result.succeeded is False
    assert result.output_path is None
    assert result.exit_code is None
    assert result.error_message == 'AutomaticLogoGenerationUnavailable'
