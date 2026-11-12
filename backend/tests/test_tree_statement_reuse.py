"""Statement reuse must execute fresh reads and bind every lookup independently."""

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.modules.kb.tree_reads import load_tree


@pytest.mark.parametrize("exact", [False, True])
def test_parallel_lookups_do_not_cache_results_or_capture_identifiers(
    exact: bool,
) -> None:
    def lookup(index: int) -> None:
        identifier = uuid4()
        key = f"SYNTHETIC-PERF001-{index}"
        session = Mock(spec=Session)
        session.execute.return_value.one_or_none.return_value = None
        for _ in range(2):
            if exact:
                assert load_tree(session, tree_id=identifier) is None
            else:
                assert load_tree(session, key=key) is None
        assert session.execute.call_count == 2
        statement, parameters = session.execute.call_args.args
        assert parameters == {"tree_lookup": identifier if exact else key}
        compiled = statement.compile(
            dialect=create_engine("postgresql+psycopg://").dialect
        )
        assert compiled.params["tree_lookup"] is None
        assert str(identifier) not in str(compiled)
        assert key not in str(compiled)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lookup, range(16)))


def test_ambiguous_selector_never_executes_a_cached_statement() -> None:
    session = Mock(spec=Session)
    with pytest.raises(ValueError):
        load_tree(session)
    with pytest.raises(ValueError):
        load_tree(session, key="SYNTHETIC", tree_id=uuid4())
    session.execute.assert_not_called()
