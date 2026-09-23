"""
Tests for scripts/configure_billing_portal.py - the one-off script that
configures the Stripe Billing Portal's subscription cancellation to
"cancel at period end", auditable in code. Full design and safety
rationale in the script's own docstring and CLAUDE.md's "Pricing/
entitlement model" section (STRIPE_PORTAL_CONFIGURATION_ID).

Uses real stripe.StripeObject fixtures (not plain mocks) for the
pagination/list-fetching layer - the same "production-shaped mocks"
lesson this project learned the hard way from the basil current_period_end
incident (see tests/test_stripe_period_end_extraction.py): a plain mock
can't reproduce StripeObject's real attribute-access behaviour
(no .get() on the raw object), so it can't catch a regression there.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from stripe._stripe_object import StripeObject

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.configure_billing_portal import (
    CANCEL_AT_PERIOD_END_OVERRIDE,
    MANAGED_BY_KEY,
    MANAGED_BY_VALUE,
    _build_target_features,
    _extract_writable_features,
    _find_default_configuration,
    _find_managed_configurations,
    _list_all_configurations,
    main,
)

FULL_RESPONSE_FEATURES = {
    "customer_update": {"enabled": True, "allowed_updates": ["email", "tax_id"]},
    "invoice_history": {"enabled": True},
    "payment_method_update": {"enabled": True, "payment_method_configuration": "pmc_real123"},
    "subscription_cancel": {
        "enabled": False,
        "mode": "immediately",
        "proration_behavior": "create_prorations",
        "cancellation_reason": {"enabled": True, "options": ["too_expensive", "unused"]},
    },
    "subscription_update": {
        "enabled": True,
        "default_allowed_updates": ["price"],
        "proration_behavior": "create_prorations",
        "billing_cycle_anchor": "unchanged",
        # Read-only/expanded-looking extras a real response can carry -
        # must never survive into a request payload unchanged.
        "products": [],
        "schedule_at_period_end": {"conditions": []},
    },
    # A field with no real-world writable meaning here, simulating an
    # unexpected/expanded key a response might carry.
    "unexpected_readonly_field": {"some": "value"},
}


def _config_payload(id, is_default=False, active=True, managed=False, features=None):
    return {
        "id": id,
        "object": "billing_portal.configuration",
        "active": active,
        "is_default": is_default,
        "livemode": True,
        "features": features if features is not None else FULL_RESPONSE_FEATURES,
        "metadata": {MANAGED_BY_KEY: MANAGED_BY_VALUE} if managed else {},
    }


def _list_response(configs, has_more=False):
    """Builds a real stripe ListObject (nested StripeObjects) - not a
    plain dict - matching what stripe.billing_portal.Configuration.list()
    actually returns."""
    payload = {"object": "list", "data": configs, "has_more": has_more}
    return StripeObject.construct_from(payload, "sk_test_x")


def _run(argv, list_pages, create_mock=None, modify_mock=None, env=None):
    """Runs main() with sys.argv patched, Configuration.list() returning
    the given pages in sequence (proving pagination is actually walked,
    not just the first page read), and create/modify mocked. Returns
    (exit_code, create_mock, modify_mock, printed_output)."""
    create_mock = create_mock or MagicMock(return_value=_fake_created_config())
    modify_mock = modify_mock or MagicMock(return_value=_fake_created_config())
    env = env or {"STRIPE_SECRET_KEY": "sk_live_test123"}
    with patch.object(sys, "argv", ["configure_billing_portal.py"] + argv), \
         patch.dict("os.environ", env, clear=False), \
         patch("scripts.configure_billing_portal.stripe.billing_portal.Configuration.list", side_effect=list_pages), \
         patch("scripts.configure_billing_portal.stripe.billing_portal.Configuration.create", create_mock), \
         patch("scripts.configure_billing_portal.stripe.billing_portal.Configuration.modify", modify_mock):
        with pytest.raises(SystemExit) as exc:
            main()
    return exc.value.code, create_mock, modify_mock


def _fake_created_config(id="bpc_new123"):
    return StripeObject.construct_from(_config_payload(id, features=FULL_RESPONSE_FEATURES), "sk_test_x")


class TestExtractWritableFeatures:
    def test_normalizes_every_sub_object_from_a_full_response(self):
        out = _extract_writable_features(FULL_RESPONSE_FEATURES)
        assert out["customer_update"] == {"enabled": True, "allowed_updates": ["email", "tax_id"]}
        assert out["invoice_history"] == {"enabled": True}
        assert out["payment_method_update"] == {"enabled": True, "payment_method_configuration": "pmc_real123"}
        assert out["subscription_update"]["enabled"] is True
        assert out["subscription_update"]["default_allowed_updates"] == ["price"]

    def test_drops_unexpected_readonly_field(self):
        out = _extract_writable_features(FULL_RESPONSE_FEATURES)
        assert "unexpected_readonly_field" not in out

    def test_handles_missing_sub_objects_without_raising(self):
        out = _extract_writable_features({})
        assert out["customer_update"] == {"enabled": False, "allowed_updates": []}
        assert out["invoice_history"] == {"enabled": False}
        assert out["subscription_cancel"]["enabled"] is False

    def test_handles_none_input_without_raising(self):
        out = _extract_writable_features(None)
        assert out["invoice_history"] == {"enabled": False}

    def test_preserves_cancellation_reason_when_present(self):
        out = _extract_writable_features(FULL_RESPONSE_FEATURES)
        assert out["subscription_cancel"]["cancellation_reason"] == {
            "enabled": True, "options": ["too_expensive", "unused"],
        }

    def test_omits_optional_fields_when_absent_rather_than_emitting_null(self):
        out = _extract_writable_features({"payment_method_update": {"enabled": True}})
        assert "payment_method_configuration" not in out["payment_method_update"]


class TestBuildTargetFeatures:
    def test_overrides_subscription_cancel_exactly(self):
        target = _build_target_features(FULL_RESPONSE_FEATURES)
        assert target["subscription_cancel"] == CANCEL_AT_PERIOD_END_OVERRIDE

    def test_does_not_preserve_old_cancellation_reason_on_override(self):
        """The approved override is the literal 3-key dict - nothing
        merged in from the source's cancellation_reason."""
        target = _build_target_features(FULL_RESPONSE_FEATURES)
        assert "cancellation_reason" not in target["subscription_cancel"]

    def test_preserves_every_other_feature_unchanged(self):
        target = _build_target_features(FULL_RESPONSE_FEATURES)
        assert target["customer_update"]["enabled"] is True
        assert target["invoice_history"]["enabled"] is True
        assert target["payment_method_update"]["enabled"] is True
        assert target["subscription_update"]["enabled"] is True


class TestListAllConfigurationsPagination:
    def test_walks_every_page_not_just_the_first(self):
        page1 = _list_response([_config_payload("bpc_1"), _config_payload("bpc_2")], has_more=True)
        page2 = _list_response([_config_payload("bpc_3", managed=True)], has_more=False)
        with patch("scripts.configure_billing_portal.stripe.billing_portal.Configuration.list", side_effect=[page1, page2]) as mock_list:
            configs = _list_all_configurations()
        assert [c["id"] for c in configs] == ["bpc_1", "bpc_2", "bpc_3"]
        assert mock_list.call_count == 2
        # Second call must paginate from the last id of the first page.
        assert mock_list.call_args_list[1].kwargs.get("starting_after") == "bpc_2"

    def test_single_page_stops_after_one_call(self):
        page1 = _list_response([_config_payload("bpc_1")], has_more=False)
        with patch("scripts.configure_billing_portal.stripe.billing_portal.Configuration.list", side_effect=[page1]) as mock_list:
            _list_all_configurations()
        assert mock_list.call_count == 1

    def test_find_managed_only_matches_the_marker(self):
        configs = [_config_payload("bpc_1", managed=False), _config_payload("bpc_2", managed=True)]
        assert [c["id"] for c in _find_managed_configurations(configs)] == ["bpc_2"]

    def test_find_default_returns_the_flagged_one(self):
        configs = [_config_payload("bpc_1", is_default=False), _config_payload("bpc_2", is_default=True)]
        assert _find_default_configuration(configs)["id"] == "bpc_2"

    def test_find_default_returns_none_when_absent(self):
        configs = [_config_payload("bpc_1", is_default=False)]
        assert _find_default_configuration(configs) is None


class TestDryRunNonMutation:
    def test_default_dry_run_never_calls_create_or_modify(self):
        page = _list_response([_config_payload("bpc_default", is_default=True)])
        code, create_mock, modify_mock = _run([], list_pages=[page])
        assert code == 0
        create_mock.assert_not_called()
        modify_mock.assert_not_called()

    def test_apply_without_live_does_not_mutate(self):
        page = _list_response([_config_payload("bpc_default", is_default=True)])
        code, create_mock, modify_mock = _run(["--apply"], list_pages=[page])
        assert code != 0
        create_mock.assert_not_called()
        modify_mock.assert_not_called()


class TestFirstCreation:
    def test_creates_when_no_managed_configuration_exists(self):
        page = _list_response([_config_payload("bpc_default", is_default=True)])
        code, create_mock, modify_mock = _run(["--apply", "--live"], list_pages=[page])
        assert code == 0
        create_mock.assert_called_once()
        modify_mock.assert_not_called()

    def test_create_payload_has_the_override_and_preserves_other_features(self):
        page = _list_response([_config_payload("bpc_default", is_default=True)])
        _, create_mock, _ = _run(["--apply", "--live"], list_pages=[page])
        sent_features = create_mock.call_args.kwargs["features"]
        assert sent_features["subscription_cancel"] == CANCEL_AT_PERIOD_END_OVERRIDE
        assert sent_features["invoice_history"]["enabled"] is True

    def test_create_tags_the_managed_metadata(self):
        page = _list_response([_config_payload("bpc_default", is_default=True)])
        _, create_mock, _ = _run(["--apply", "--live"], list_pages=[page])
        assert create_mock.call_args.kwargs["metadata"] == {MANAGED_BY_KEY: MANAGED_BY_VALUE}


class TestRepeatedRunNoOp:
    def test_existing_active_managed_without_update_is_a_no_op(self):
        page = _list_response([
            _config_payload("bpc_default", is_default=True),
            _config_payload("bpc_managed", managed=True, active=True),
        ])
        code, create_mock, modify_mock = _run(["--apply", "--live"], list_pages=[page])
        assert code == 0
        create_mock.assert_not_called()
        modify_mock.assert_not_called()

    def test_dry_run_also_reports_no_op_without_erroring(self):
        page = _list_response([_config_payload("bpc_managed", managed=True, active=True)])
        code, create_mock, modify_mock = _run([], list_pages=[page])
        assert code == 0
        create_mock.assert_not_called()
        modify_mock.assert_not_called()


class TestExplicitUpdate:
    def test_update_flag_calls_modify_not_create(self):
        page = _list_response([
            _config_payload("bpc_default", is_default=True),
            _config_payload("bpc_managed", managed=True, active=True),
        ])
        code, create_mock, modify_mock = _run(["--apply", "--live", "--update"], list_pages=[page])
        assert code == 0
        modify_mock.assert_called_once()
        create_mock.assert_not_called()
        assert modify_mock.call_args.args[0] == "bpc_managed"

    def test_update_payload_still_uses_the_override(self):
        page = _list_response([
            _config_payload("bpc_default", is_default=True),
            _config_payload("bpc_managed", managed=True, active=True),
        ])
        _, _, modify_mock = _run(["--apply", "--live", "--update"], list_pages=[page])
        assert modify_mock.call_args.kwargs["features"]["subscription_cancel"] == CANCEL_AT_PERIOD_END_OVERRIDE


class TestDuplicateManagedConfigurations:
    def test_two_managed_configurations_fails_without_choosing(self):
        page = _list_response([
            _config_payload("bpc_managed_a", managed=True),
            _config_payload("bpc_managed_b", managed=True),
        ])
        code, create_mock, modify_mock = _run(["--apply", "--live"], list_pages=[page])
        assert code != 0
        create_mock.assert_not_called()
        modify_mock.assert_not_called()

    def test_duplicate_ids_are_printed(self, capsys):
        page = _list_response([
            _config_payload("bpc_managed_a", managed=True),
            _config_payload("bpc_managed_b", managed=True),
        ])
        _run(["--apply", "--live"], list_pages=[page])
        out = capsys.readouterr().out
        assert "bpc_managed_a" in out
        assert "bpc_managed_b" in out


class TestInactiveManagedConfiguration:
    def test_inactive_managed_configuration_fails_and_does_not_mutate(self):
        page = _list_response([_config_payload("bpc_managed", managed=True, active=False)])
        code, create_mock, modify_mock = _run(["--apply", "--live", "--update"], list_pages=[page])
        assert code != 0
        create_mock.assert_not_called()
        modify_mock.assert_not_called()

    def test_inactive_managed_configuration_fails_even_without_update(self):
        page = _list_response([_config_payload("bpc_managed", managed=True, active=False)])
        code, _, _ = _run(["--apply", "--live"], list_pages=[page])
        assert code != 0


class TestMissingDefaultConfiguration:
    def test_no_default_and_no_allow_baseline_fails(self):
        page = _list_response([_config_payload("bpc_random", is_default=False)])
        code, create_mock, modify_mock = _run(["--apply", "--live"], list_pages=[page])
        assert code != 0
        create_mock.assert_not_called()
        modify_mock.assert_not_called()

    def test_no_default_with_allow_baseline_proceeds(self):
        page = _list_response([_config_payload("bpc_random", is_default=False)])
        code, create_mock, _ = _run(["--apply", "--live", "--allow-baseline"], list_pages=[page])
        assert code == 0
        create_mock.assert_called_once()
        sent_features = create_mock.call_args.kwargs["features"]
        assert sent_features["invoice_history"]["enabled"] is True
        assert sent_features["subscription_cancel"] == CANCEL_AT_PERIOD_END_OVERRIDE

    def test_completely_empty_account_with_allow_baseline_proceeds(self):
        page = _list_response([])
        code, create_mock, _ = _run(["--apply", "--live", "--allow-baseline"], list_pages=[page])
        assert code == 0
        create_mock.assert_called_once()


class TestLiveKeyEnforcement:
    def test_apply_with_test_key_is_refused(self):
        page = _list_response([_config_payload("bpc_default", is_default=True)])
        code, create_mock, modify_mock = _run(
            ["--apply", "--live"], list_pages=[page], env={"STRIPE_SECRET_KEY": "sk_test_notlive"},
        )
        assert code != 0
        create_mock.assert_not_called()
        modify_mock.assert_not_called()

    def test_missing_key_is_refused(self):
        page = _list_response([_config_payload("bpc_default", is_default=True)])
        code, create_mock, modify_mock = _run(["--apply", "--live"], list_pages=[page], env={"STRIPE_SECRET_KEY": ""})
        assert code != 0
        create_mock.assert_not_called()

    def test_dry_run_does_not_require_a_live_key(self):
        """A dry run should still be usable to preview against a test
        key without being refused - only --apply enforces liveness."""
        page = _list_response([_config_payload("bpc_default", is_default=True)])
        code, create_mock, modify_mock = _run([], list_pages=[page], env={"STRIPE_SECRET_KEY": "sk_test_preview"})
        assert code == 0
        create_mock.assert_not_called()


class TestCreateRequestIdempotency:
    def test_create_call_includes_an_idempotency_key(self):
        page = _list_response([_config_payload("bpc_default", is_default=True)])
        _, create_mock, _ = _run(["--apply", "--live"], list_pages=[page])
        assert "idempotency_key" in create_mock.call_args.kwargs
        assert create_mock.call_args.kwargs["idempotency_key"]

    def test_idempotency_key_differs_across_separate_runs(self):
        """Confirms it's freshly generated per invocation, not a fixed
        constant - a fixed key would itself dedupe separate real runs,
        which is explicitly the job of the metadata marker, not this."""
        page1 = _list_response([_config_payload("bpc_default", is_default=True)])
        page2 = _list_response([_config_payload("bpc_default", is_default=True)])
        _, create_mock_1, _ = _run(["--apply", "--live"], list_pages=[page1])
        _, create_mock_2, _ = _run(["--apply", "--live"], list_pages=[page2])
        key1 = create_mock_1.call_args.kwargs["idempotency_key"]
        key2 = create_mock_2.call_args.kwargs["idempotency_key"]
        assert key1 != key2

    def test_modify_call_does_not_use_an_idempotency_key(self):
        """Idempotency for the update path is the target configuration
        id itself (PUT-like semantics onto an existing resource) - the
        create-specific idempotency_key isn't meaningful there."""
        page = _list_response([
            _config_payload("bpc_default", is_default=True),
            _config_payload("bpc_managed", managed=True, active=True),
        ])
        _, _, modify_mock = _run(["--apply", "--live", "--update"], list_pages=[page])
        assert "idempotency_key" not in modify_mock.call_args.kwargs


class TestNoSecretsOrRawObjectsPrinted:
    def test_dry_run_output_never_prints_the_api_key(self, capsys):
        page = _list_response([_config_payload("bpc_default", is_default=True)])
        _run([], list_pages=[page], env={"STRIPE_SECRET_KEY": "sk_live_SUPERSECRETVALUE"})
        out = capsys.readouterr().out
        assert "SUPERSECRETVALUE" not in out

    def test_apply_output_never_prints_the_api_key(self, capsys):
        page = _list_response([_config_payload("bpc_default", is_default=True)])
        _run(["--apply", "--live"], list_pages=[page], env={"STRIPE_SECRET_KEY": "sk_live_SUPERSECRETVALUE"})
        out = capsys.readouterr().out
        assert "SUPERSECRETVALUE" not in out
