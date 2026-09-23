"""
One-off script to configure the Stripe Billing Portal's subscription
cancellation to "cancel at period end" (mode=at_period_end,
proration_behavior=none - the only valid proration value for that mode;
create_prorations is only compatible with mode=immediately, and
always_invoice errors outright), auditable in code rather than left to
whatever the Stripe Dashboard currently defaults to.

Produces the bpc_... configuration ID that STRIPE_PORTAL_CONFIGURATION_ID
(see app.py's billing_portal(), CLAUDE.md's "Pricing/entitlement model")
should be set to. This script never sets that env var itself - no Render
access exists in this environment, and the two steps (creating the
Stripe configuration, and pointing the app at it) are kept deliberately
separate even where they could technically be automated.

Usage:
    python scripts/configure_billing_portal.py                          # dry run (default, safe, no mutation)
    python scripts/configure_billing_portal.py --apply --live           # create the configuration for real
    python scripts/configure_billing_portal.py --apply --live --update  # refresh the existing managed configuration instead
    python scripts/configure_billing_portal.py --apply --live --allow-baseline  # only if no default configuration exists at all

Safety properties (each covered by a test in tests/test_configure_billing_portal.py):
  - Read-only by default. --apply is required for any
    Configuration.create()/.modify() call.
  - --live is required IN ADDITION to --apply for the real operation - a
    second, explicit acknowledgement, separate from the (also enforced)
    sk_live_ key check.
  - Never submits Stripe's own response object as a request payload -
    every field is re-built through an explicit allowlist
    (_extract_writable_features), since response objects can contain
    null/expanded/read-only fields a request would reject or mishandle.
  - Finds existing managed configurations via full pagination, not just
    the first page (_list_all_configurations).
  - Multiple managed configurations found -> fails safely, lists only
    their IDs, makes no choice on your behalf.
  - Exactly one managed configuration found but inactive -> fails and
    requires an explicit decision, rather than silently reusing it.
  - Exactly one managed configuration found and active, --update not
    passed -> reports it and exits cleanly (idempotent no-op) - no API
    mutation call is made at all.
  - No default configuration exists -> fails by default; only proceeds
    with an explicit fallback baseline via --allow-baseline, so a
    genuinely missing default can never silently produce a
    reduced-functionality portal.
  - A fresh idempotency_key is generated per run for the individual
    create request (protects against a network retry duplicating THAT
    request) - deliberately separate from the metadata-based discovery
    above, which is what protects against re-running the script on a
    later day.
  - Never prints a secret, customer data, or a complete raw Stripe
    object - only configuration IDs and a small sanitised
    feature-enabled summary.
"""
import argparse
import os
import sys
import uuid

import stripe

MANAGED_BY_KEY = "managed_by"
MANAGED_BY_VALUE = "spendara_configure_billing_portal_v1"
CONFIGURATION_NAME = "Spendara - cancel at period end"

# The literal override this script exists to apply - not derived from
# anything, per the approved plan.
CANCEL_AT_PERIOD_END_OVERRIDE = {
    "enabled": True,
    "mode": "at_period_end",
    "proration_behavior": "none",
}

# Only used with --allow-baseline, when no default configuration exists
# on the account at all. Deliberately explicit and minimal rather than
# guessed - logged clearly whenever it's actually used.
FALLBACK_BASELINE_FEATURES = {
    "customer_update": {"enabled": False, "allowed_updates": []},
    "invoice_history": {"enabled": True},
    "payment_method_update": {"enabled": True},
    "subscription_update": {"enabled": False, "default_allowed_updates": [], "proration_behavior": "none"},
}


def _extract_writable_features(features):
    """Takes a Stripe `features` dict (a response shape, which may
    contain null/expanded/read-only fields a request would reject) and
    returns a NEW dict containing only the documented writable request
    fields, with their current values preserved. Never passes a Stripe
    response object straight back into a request - this is that
    boundary."""
    features = features or {}
    out = {}

    cu = features.get("customer_update") or {}
    out["customer_update"] = {
        "enabled": bool(cu.get("enabled", False)),
        "allowed_updates": list(cu.get("allowed_updates") or []),
    }

    ih = features.get("invoice_history") or {}
    out["invoice_history"] = {"enabled": bool(ih.get("enabled", False))}

    pmu = features.get("payment_method_update") or {}
    pmu_out = {"enabled": bool(pmu.get("enabled", False))}
    if pmu.get("payment_method_configuration"):
        pmu_out["payment_method_configuration"] = pmu["payment_method_configuration"]
    out["payment_method_update"] = pmu_out

    sc = features.get("subscription_cancel") or {}
    sc_out = {
        "enabled": bool(sc.get("enabled", False)),
        "mode": sc.get("mode") or "at_period_end",
        "proration_behavior": sc.get("proration_behavior") or "none",
    }
    cr = sc.get("cancellation_reason") or {}
    if cr:
        cr_out = {"enabled": bool(cr.get("enabled", False)), "options": list(cr.get("options") or [])}
        if cr.get("feedback_options"):
            cr_out["feedback_options"] = list(cr["feedback_options"])
        sc_out["cancellation_reason"] = cr_out
    out["subscription_cancel"] = sc_out

    su = features.get("subscription_update") or {}
    su_out = {
        "enabled": bool(su.get("enabled", False)),
        "default_allowed_updates": list(su.get("default_allowed_updates") or []),
        "proration_behavior": su.get("proration_behavior") or "none",
    }
    if su.get("billing_cycle_anchor"):
        su_out["billing_cycle_anchor"] = su["billing_cycle_anchor"]
    if su.get("products"):
        products_out = []
        for p in su["products"]:
            p_out = {"product": p.get("product"), "prices": list(p.get("prices") or [])}
            aq = p.get("adjustable_quantity")
            if aq:
                aq_out = {"enabled": bool(aq.get("enabled", False))}
                if aq.get("maximum") is not None:
                    aq_out["maximum"] = aq["maximum"]
                if aq.get("minimum") is not None:
                    aq_out["minimum"] = aq["minimum"]
                p_out["adjustable_quantity"] = aq_out
            products_out.append(p_out)
        su_out["products"] = products_out
    sched = su.get("schedule_at_period_end") or {}
    if sched.get("conditions"):
        su_out["schedule_at_period_end"] = {
            "conditions": [{"type": c["type"]} for c in sched["conditions"] if c.get("type")]
        }
    if su.get("trial_update_behavior"):
        su_out["trial_update_behavior"] = su["trial_update_behavior"]
    out["subscription_update"] = su_out

    return out


def _build_target_features(source_features):
    """Preserves every currently-live feature (through the allowlisted
    serializer above), overriding only subscription_cancel with the
    literal, approved cancel-at-period-end settings."""
    features = _extract_writable_features(source_features)
    features["subscription_cancel"] = dict(CANCEL_AT_PERIOD_END_OVERRIDE)
    return features


def _list_all_configurations():
    """Paginates through every existing Configuration - never assumes a
    matching managed configuration is on the first page. Converts each
    result to a plain dict immediately (StripeObject has no .get() -
    only __getitem__/attribute access on the raw object)."""
    configs = []
    starting_after = None
    while True:
        kwargs = {"limit": 100}
        if starting_after:
            kwargs["starting_after"] = starting_after
        page = stripe.billing_portal.Configuration.list(**kwargs)
        page_data = page["data"]
        configs.extend(item.to_dict() for item in page_data)
        if not page["has_more"]:
            break
        starting_after = page_data[-1]["id"]
    return configs


def _find_managed_configurations(all_configs):
    return [c for c in all_configs if (c.get("metadata") or {}).get(MANAGED_BY_KEY) == MANAGED_BY_VALUE]


def _find_default_configuration(all_configs):
    defaults = [c for c in all_configs if c.get("is_default")]
    return defaults[0] if defaults else None


def _print_feature_summary(features):
    print("  customer_update.enabled:", features["customer_update"]["enabled"])
    print("  invoice_history.enabled:", features["invoice_history"]["enabled"])
    print("  payment_method_update.enabled:", features["payment_method_update"]["enabled"])
    sc = features["subscription_cancel"]
    print(f"  subscription_cancel: enabled={sc['enabled']} mode={sc['mode']} proration_behavior={sc['proration_behavior']}")
    print("  subscription_update.enabled:", features["subscription_update"]["enabled"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Without this, dry-run only - reports the intended action, mutates nothing.")
    parser.add_argument("--live", action="store_true", help="Required in addition to --apply for the real operation - an explicit second acknowledgement.")
    parser.add_argument("--update", action="store_true", help="Refresh the existing single managed configuration instead of treating it as a no-op.")
    parser.add_argument("--allow-baseline", action="store_true", help="Only needed if no default configuration exists at all - uses an explicit, logged fallback baseline instead of failing.")
    args = parser.parse_args()

    stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
    if not stripe.api_key:
        print("STRIPE_SECRET_KEY not set in this environment.")
        sys.exit(1)

    if args.apply and not args.live:
        print("--apply requires --live as an explicit second acknowledgement. Refusing to proceed.")
        sys.exit(1)

    if args.apply and not stripe.api_key.startswith("sk_live_"):
        print("Refusing: --apply requires a live secret key (sk_live_...). The configured key does not start with sk_live_.")
        sys.exit(1)

    all_configs = _list_all_configurations()
    managed = _find_managed_configurations(all_configs)

    if len(managed) > 1:
        print(f"Multiple managed configurations found ({len(managed)}) - refusing to choose. IDs:")
        for c in managed:
            print(f"  {c['id']}")
        sys.exit(1)

    existing_managed = managed[0] if managed else None

    if existing_managed and not existing_managed.get("active"):
        print(f"Managed configuration {existing_managed['id']} exists but is INACTIVE. "
              "Refusing to silently reuse it - reactivate or remove it in the Stripe "
              "Dashboard first, then re-run.")
        sys.exit(1)

    if existing_managed and not args.update:
        print(f"Managed configuration already exists: {existing_managed['id']} (active). "
              "Nothing to do - pass --update to refresh its settings.")
        sys.exit(0)

    default_config = _find_default_configuration(all_configs)
    if default_config is not None:
        source_features = default_config["features"]
        source_label = f"default configuration {default_config['id']}"
    elif args.allow_baseline:
        source_features = FALLBACK_BASELINE_FEATURES
        source_label = "explicit --allow-baseline fallback (NO default configuration exists on this account)"
        print(f"WARNING: no default Billing Portal configuration exists - using {source_label}.")
    else:
        print("No default Billing Portal configuration exists on this account, and --allow-baseline "
              "was not passed. Refusing to guess at a baseline that could silently reduce portal "
              "functionality for real customers. Pass --allow-baseline to proceed with the explicit, "
              "logged fallback instead.")
        sys.exit(1)

    target_features = _build_target_features(source_features)

    action = "UPDATE" if existing_managed else "CREATE"
    print(f"{'APPLY' if args.apply else 'DRY RUN'}: would {action} a Billing Portal configuration (source: {source_label})")
    _print_feature_summary(target_features)

    if not args.apply:
        print("Dry run only - re-run with --apply --live to write this change.")
        sys.exit(0)

    if existing_managed:
        result = stripe.billing_portal.Configuration.modify(
            existing_managed["id"],
            features=target_features,
            metadata={MANAGED_BY_KEY: MANAGED_BY_VALUE},
        )
    else:
        idempotency_key = str(uuid.uuid4())
        result = stripe.billing_portal.Configuration.create(
            features=target_features,
            name=CONFIGURATION_NAME,
            metadata={MANAGED_BY_KEY: MANAGED_BY_VALUE},
            idempotency_key=idempotency_key,
        )

    result_dict = result.to_dict()
    print(f"{action} complete. Configuration id: {result_dict['id']}")
    _print_feature_summary(_extract_writable_features(result_dict.get("features")))
    sys.exit(0)


if __name__ == "__main__":
    main()
