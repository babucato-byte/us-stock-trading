"""CODEX-052: the KIS wire-format verification status must be stated once,
consistently, and must match both the constants actually used and the
Oracle runbook.

The defect this replaces: `brokers/kis_broker.py` carried a
"to be verified" comment saying the general cancel TR_ID and the price
response field were NOT confirmed, while the same file's module
docstring said both HAD been confirmed against the official reference
repository. Both statements described the same values. An operator
reading the file could not tell what still needed checking on Oracle.

The two statements were about different axes -- reference-verified vs
live-response-confirmed -- and neither said so. VERIFICATION_MATRIX now
states both axes explicitly, and these tests keep prose, constants and
runbook from drifting apart again.
"""
import pathlib

import pytest

from brokers import kis_broker

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE = (REPO_ROOT / "brokers" / "kis_broker.py").read_text(encoding="utf-8")
RUNBOOK = (REPO_ROOT / "docs" / "deployment" / "ORACLE_KIS_MIGRATION_RUNBOOK.md").read_text(
    encoding="utf-8"
)

REFERENCE_STATUSES = {kis_broker.REFERENCE_VERIFIED, kis_broker.REFERENCE_UNVERIFIED}
LIVE_STATUSES = {kis_broker.LIVE_RESPONSE_CONFIRMED, kis_broker.LIVE_RESPONSE_PENDING}


class TestMatrixIsWellFormed:
    def test_matrix_is_not_empty(self):
        assert kis_broker.VERIFICATION_MATRIX

    def test_every_entry_uses_the_declared_vocabulary(self):
        for entry in kis_broker.VERIFICATION_MATRIX:
            assert entry.reference_status in REFERENCE_STATUSES, entry
            assert entry.live_status in LIVE_STATUSES, entry

    def test_every_entry_names_its_source(self):
        for entry in kis_broker.VERIFICATION_MATRIX:
            assert entry.source.strip(), entry

    def test_entry_names_are_unique(self):
        names = [entry.name for entry in kis_broker.VERIFICATION_MATRIX]
        assert len(names) == len(set(names))

    def test_pending_list_is_derived_from_the_matrix(self):
        expected = tuple(
            entry.name for entry in kis_broker.VERIFICATION_MATRIX
            if entry.live_status == kis_broker.LIVE_RESPONSE_PENDING
        )
        assert kis_broker.LIVE_RESPONSE_PENDING_ITEMS == expected


class TestNoContradictoryStatusMarkers:
    def test_the_ambiguous_marker_is_gone(self):
        """The old marker meant "somebody must check this against the live
        docs" without saying which axis was unmet -- which is exactly how
        it ended up contradicting the docstring."""
        assert "TBD_VERIFY_LIVE_DOCS" not in SOURCE

    def test_no_prose_claims_a_pending_item_was_directly_confirmed(self):
        """A file cannot say a value is both unconfirmed and confirmed."""
        contradictions = [
            "was not directly\n        confirmed",
            "was not directly confirmed",
            "not directly confirmed from the fetched source",
        ]
        for phrase in contradictions:
            assert phrase not in SOURCE, f"contradictory prose survives: {phrase!r}"

    def test_the_runbook_no_longer_references_the_old_marker(self):
        assert "TBD_VERIFY_LIVE_DOCS" not in RUNBOOK


class TestValuesAreUnchanged:
    """CODEX-052 was a documentation fix. If any of these moved, the
    change was NOT documentation-only and needs its own verification."""

    def test_cancel_tr_id_pair(self):
        assert kis_broker.TR_ID_CANCEL == {"live": "TTTT1004U", "paper": "VTTT1004U"}

    def test_order_tr_ids(self):
        assert kis_broker.TR_ID_ORDER_US == {
            ("live", "buy"): "TTTT1002U", ("paper", "buy"): "VTTT1002U",
            ("live", "sell"): "TTTT1006U", ("paper", "sell"): "VTTT1001U",
        }

    def test_paths(self):
        assert kis_broker.CANCEL_PATH == "/uapi/overseas-stock/v1/trading/order-rvsecncl"
        assert kis_broker.ORDER_PATH == "/uapi/overseas-stock/v1/trading/order"
        assert kis_broker.PRICE_PATH == "/uapi/overseas-price/v1/quotations/price"

    def test_exchange_code_spaces_stay_distinct(self):
        assert kis_broker._EXCHANGE_TO_EXCD == {"NASDAQ": "NAS", "NYSE": "NYS", "AMEX": "AMS"}
        assert kis_broker._EXCHANGE_TO_ORDER_EXCG_CD == {
            "NASDAQ": "NASD", "NYSE": "NYSE", "AMEX": "AMEX",
        }

    def test_price_field_is_still_output_last(self):
        assert 'output.get("last")' in SOURCE

    def test_matrix_values_match_the_constants_in_use(self):
        by_name = {entry.name: entry.value for entry in kis_broker.VERIFICATION_MATRIX}
        assert by_name["cancel_tr_id_live"] == kis_broker.TR_ID_CANCEL["live"]
        assert by_name["cancel_tr_id_paper"] == kis_broker.TR_ID_CANCEL["paper"]
        assert by_name["order_tr_id_live_buy"] == kis_broker.TR_ID_ORDER_US[("live", "buy")]
        assert by_name["cancel_path"] == kis_broker.CANCEL_PATH
        assert by_name["order_path"] == kis_broker.ORDER_PATH
        assert by_name["price_path"] == kis_broker.PRICE_PATH


class TestRunbookMatchesTheMatrix:
    def test_runbook_documents_the_two_axes(self):
        assert "REFERENCE_VERIFIED" in RUNBOOK
        assert "LIVE_RESPONSE_PENDING" in RUNBOOK
        assert "VERIFICATION_MATRIX" in RUNBOOK

    def test_runbook_names_the_two_items_that_must_be_confirmed_live(self):
        # Both remain named in the runbook: price_field_last as the
        # OBSERVE value a read-only probe confirmed, cancel_tr_id_live as
        # one ARMED still waits on.
        assert "price_field_last" in RUNBOOK
        assert "cancel_tr_id_live" in RUNBOOK

    def test_runbook_tells_the_operator_how_to_list_every_pending_item(self):
        """It now lists them PER POSTURE, through the accessors, rather
        than iterating the raw matrix -- so the runbook and preflight
        cannot disagree about what a posture needs."""
        assert "LIVE_RESPONSE_PENDING" in RUNBOOK or "pending_items_for" in RUNBOOK
        assert "from brokers.kis_broker import (" in RUNBOOK
        assert "pending_items_for" in RUNBOOK
        assert "matrix_entries_for" in RUNBOOK

    def test_runbook_documents_the_posture_split(self):
        assert "OBSERVE_REQUIRED" in RUNBOOK
        assert "ARMED_REQUIRED" in RUNBOOK
        assert "order_exchange_code_space" in RUNBOOK

    def test_runbook_forbids_confirming_live_only_ids_from_paper(self):
        collapsed = " ".join(RUNBOOK.split())
        assert "모의투자 응답으로 확인 처리하면 안 된다" in collapsed

    def test_runbook_forbids_confirming_the_cancel_tr_id_with_a_real_order(self):
        assert "모의투자" in RUNBOOK
        # Collapse wrapping before matching -- the runbook hard-wraps.
        collapsed = " ".join(RUNBOOK.split())
        assert "실계좌 주문으로 확인하지 않는다" in collapsed

    @pytest.mark.parametrize("item", ["cancel_tr_id_live", "order_tr_id_live_buy"])
    def test_named_items_are_actually_pending_in_the_matrix(self, item):
        """The live-only TR_IDs: no paper response and no document can
        confirm these, so they stay pending until a sanctioned live
        probe, and ARMED stays blocked."""
        assert item in kis_broker.LIVE_RESPONSE_PENDING_ITEMS

    def test_the_observe_values_are_confirmed_by_a_real_response(self):
        """price_field_last moved to CONFIRMED on the strength of a live
        read-only probe, not a document."""
        by_name = {entry.name: entry for entry in kis_broker.VERIFICATION_MATRIX}
        for name in ("price_path", "price_field_last", "order_exchange_code_space"):
            entry = by_name[name]
            assert entry.live_status == kis_broker.LIVE_RESPONSE_CONFIRMED, name
            assert "probe" in entry.source, f"{name} cites no live evidence"

    def test_every_observe_value_cites_live_evidence(self):
        """The stronger form: whatever OBSERVE requires, a real response
        established it. ORACLE-CASH-01 is why -- the cash contract was
        never in the matrix, so nothing ever compared the field names the
        code used against a response, and a name that does not exist
        survived as a confident $0."""
        for entry in kis_broker.matrix_entries_for(kis_broker.REQUIRED_FOR_OBSERVE):
            assert entry.live_status == kis_broker.LIVE_RESPONSE_CONFIRMED, entry.name
            assert "probe" in entry.source, f"{entry.name} cites no live evidence"

    def test_the_orderable_amount_contract_is_in_the_matrix(self):
        """The three values entry sizing depends on, plus the disproved
        one, recorded so the wrong field cannot quietly return."""
        by_name = {entry.name: entry for entry in kis_broker.VERIFICATION_MATRIX}
        assert by_name["orderable_amount_path"].value == kis_broker.PSAMOUNT_PATH
        assert by_name["orderable_amount_tr_id_live"].value == kis_broker.TR_ID_PSAMOUNT["live"]
        assert by_name["orderable_amount_field"].value == (
            f"output.{kis_broker.ORDERABLE_AMOUNT_FIELD}")
        assert "balance_cash_fields_absent" in by_name
        for name in ("orderable_amount_path", "orderable_amount_tr_id_live",
                     "orderable_amount_field", "balance_cash_fields_absent"):
            assert kis_broker.REQUIRED_FOR_OBSERVE in by_name[name].required_for, name

    def test_the_paper_orderable_tr_id_is_not_claimed_as_confirmed(self):
        """A live probe confirms the LIVE TR_ID only. Claiming the paper
        one on the same evidence is the mistake the ARMED items exist to
        prevent."""
        values = {entry.value for entry in kis_broker.VERIFICATION_MATRIX
                  if entry.live_status == kis_broker.LIVE_RESPONSE_CONFIRMED}
        assert kis_broker.TR_ID_PSAMOUNT["paper"] not in values

    def test_armed_still_waits_on_exactly_the_live_order_and_cancel_values(self):
        """Adding OBSERVE requirements must not quietly un-gate ARMED.

        `cancel_tr_id_paper` is deliberately absent: it is the PAPER
        cancel TR, which no live path reads, so it is tracked under its
        own scope instead of gating live eligibility. Its evidence is
        still pending -- see the paper-scope test below."""
        assert set(kis_broker.pending_items_for(kis_broker.REQUIRED_FOR_ARMED)) == {
            "order_path", "order_tr_id_live_buy", "cancel_path",
            "cancel_tr_id_live", "cancel_price_field_rule",
        }
        assert kis_broker.pending_items_for(kis_broker.REQUIRED_FOR_OBSERVE) == ()

    def test_the_paper_value_is_still_tracked_and_still_unconfirmed(self):
        assert set(kis_broker.pending_items_for(kis_broker.REQUIRED_FOR_PAPER)) == {
            "cancel_tr_id_paper"}


class TestVerificationStatusIsNotARuntimeSwitch:
    def test_matrix_does_not_gate_any_behaviour(self):
        """The matrix is documentation-as-data. If execution ever branched
        on it, editing documentation would change trading behaviour."""
        import ast

        tree = ast.parse(SOURCE)

        def _reads_matrix(node):
            return any(
                isinstance(child, ast.Name) and child.id in (
                    "VERIFICATION_MATRIX", "LIVE_RESPONSE_PENDING_ITEMS",
                )
                for child in ast.walk(node)
            )

        for node in ast.walk(tree):
            if isinstance(node, (ast.If, ast.While, ast.IfExp)):
                assert not _reads_matrix(node.test), (
                    f"line {node.lineno}: control flow branches on the verification matrix"
                )
            # No function body may read it either, with one exception:
            # the two pure accessors that exist so preflight has a single
            # authority to ask. They RETURN matrix rows; they contain no
            # branch on a status, which the control-flow check above
            # enforces for every node including theirs. Broker behaviour
            # still cannot consult the matrix.
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in ("matrix_entries_for", "pending_items_for"):
                    continue
                assert not _reads_matrix(node), (
                    f"{node.name}() reads the verification matrix at runtime"
                )

    def test_the_accessors_only_filter_and_never_branch_on_status(self):
        """The exemption above is safe only while those two functions
        stay pure projections of the matrix."""
        import ast

        tree = ast.parse(SOURCE)
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name not in ("matrix_entries_for", "pending_items_for"):
                continue
            for child in ast.walk(node):
                assert not isinstance(child, (ast.If, ast.While)), (
                    f"{node.name}() branches; it must only filter"
                )
            calls = [c for c in ast.walk(node) if isinstance(c, ast.Call)]
            for call in calls:
                name = getattr(call.func, "id", None) or getattr(call.func, "attr", None)
                assert name in ("tuple", "matrix_entries_for"), (
                    f"{node.name}() calls {name}(); it must only filter"
                )

    def test_no_broker_method_consults_the_matrix(self):
        """The property that actually matters: editing documentation must
        not change what the broker does."""
        import ast

        tree = ast.parse(SOURCE)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or node.name != "KISBroker":
                continue
            for child in ast.walk(node):
                if isinstance(child, ast.Name) and child.id in (
                    "VERIFICATION_MATRIX", "LIVE_RESPONSE_PENDING_ITEMS",
                ):
                    raise AssertionError("KISBroker reads the verification matrix")
