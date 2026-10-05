import pytest
from pytest_bdd import given, scenarios, then, when
import test_polymarket as cases

pytestmark = pytest.mark.bdd
scenarios("../features/polymarket_connector.feature")


@given("a public Polymarket fixture")
def public_fixture():
    pass


@when("keyset discovery resumes and a page is retried")
def discover(tmp_path):
    cases.test_pagination_identity_and_budget(tmp_path)


@then("public identities and raw snapshots are preserved")
def preserved():
    pass


@when("the provider fails repeatedly")
def fails(tmp_path):
    cases.test_provider_retries_are_bounded(tmp_path)


@then("retries stop at the campaign request budget")
def bounded():
    pass


@when("settlement and outcome definitions are checked")
def definitions():
    cases.test_ambiguous_and_unsupported_contracts_stay_unknown()


@then("only an exact supported outcome can be priced")
def exact():
    pass


@when("a selected asset feed disconnects")
def disconnect(tmp_path):
    cases.test_book_identity_fees_ticks_and_feed_gap(tmp_path)


@then("coherent book publication stops until resynchronization")
def stopped():
    pass
