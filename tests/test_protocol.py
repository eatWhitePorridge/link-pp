from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from handoff import gateway as gateway_module
from handoff.countries import get_country
from handoff.gateway import (
    CheckoutArtifact,
    LiveProtocolGateway,
    resolve_paypal_approval_url,
)
from handoff.proxies import parse_proxy_lines
from handoff.protocol import stripe_checkout as stripe


def test_checkout_response_prefers_oaics_and_sends_country_promo_contract(monkeypatch):
    captured = {}

    class Response:
        status_code = 200
        text = json.dumps(
            {
                "checkout_session_id": "oaics_dynamic",
                "processor_entity": "openai_ie",
                "data": {"checkout_session_id": "cs_live_nested"},
            }
        )

    class Http:
        def post(self, *_args, **kwargs):
            captured["url"] = _args[0]
            captured.update(kwargs)
            return Response()

    monkeypatch.setattr(stripe, "_warmup_chatgpt_page", lambda *_args, **_kwargs: None)
    context = {"stale": "value"}
    session_id, error = stripe.create_chatgpt_order(
        Http(),
        "test-at",
        country="DE",
        currency="EUR",
        checkout_context=context,
        with_promo=True,
    )

    assert error is None
    assert session_id == "oaics_dynamic"
    assert context["checkout_kind"] == "oaics"
    assert context["processor_entity"] == "openai_ie"
    assert "stale" not in context
    assert captured["json"]["billing_details"] == {
        "country": "DE",
        "currency": "EUR",
    }
    assert captured["json"]["checkout_ui_mode"] == "custom"
    assert captured["json"]["promo_campaign"] == {
        "promo_campaign_id": "plus-1-month-free",
        "is_coupon_from_query_param": False,
    }
    assert captured["headers"]["x-openai-target-route"] == "/backend-api/payments/checkout"
    assert captured["url"] == stripe.OPENAI_CHECKOUT_URL
    assert not captured["url"].endswith("/update")


def test_checkout_transport_retry_rebuilds_session_and_preserves_diagnostics(monkeypatch):
    first_http = object()
    second_http = object()
    calls = []
    logs = []

    def create_order(http, access_token, **_kwargs):
        calls.append((http, access_token))
        if len(calls) == 1:
            return (
                None,
                "request_error: ConnectionError: curl: (56) "
                "OPENSSL_internal:BAD_DECRYPT Bearer secret-at",
            )
        return "oaics_recovered", None

    monkeypatch.setattr(stripe, "create_chatgpt_order", create_order)
    monkeypatch.setattr(stripe.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(stripe.random, "uniform", lambda *_args: 0.1)

    session_id, error = stripe.create_chatgpt_order_with_retry(
        first_http,
        "secret-at",
        max_attempts=2,
        renew_http=lambda current: second_http if current is first_http else None,
        log=logs.append,
    )

    assert session_id == "oaics_recovered"
    assert error is None
    assert [item[0] for item in calls] == [first_http, second_http]
    assert any("BAD_DECRYPT" in message for message in logs)
    assert any("重建同代理 HTTP 会话" in message for message in logs)
    assert "secret-at" not in repr(logs)


def test_renew_http_session_copies_cookies_and_closes_old(monkeypatch):
    events = []

    class CookieJar:
        def __init__(self, values):
            self.values = dict(values)

        def update(self, other):
            events.append(("cookies", dict(other.values)))
            self.values.update(other.values)

    class Http:
        def __init__(self, values):
            self.cookies = CookieJar(values)

        def close(self):
            events.append(("close", self))

    old = Http({"oai-did": "device-test", "__cf_bm": "cookie-test"})
    replacement = Http({})
    monkeypatch.setattr(stripe, "build_http", lambda proxy: events.append(("build", proxy)) or replacement)

    result = stripe.renew_http_session(old, "socks5://proxy.example:1000")

    assert result is replacement
    assert replacement.cookies.values == old.cookies.values
    assert events == [
        ("build", "socks5://proxy.example:1000"),
        ("cookies", old.cookies.values),
        ("close", old),
    ]


def test_stripe_flow_uses_supplied_publishable_key_without_probe(monkeypatch):
    calls = []
    context = {"payment_method_types": ["paypal"], "checkout_amount": 0, "currency": "usd"}
    monkeypatch.setattr(
        stripe,
        "verify_pk",
        lambda *_args: (_ for _ in ()).throw(AssertionError("fixed-key probe must not run")),
    )
    monkeypatch.setattr(
        stripe,
        "init_checkout",
        lambda _http, _session, pk, *_args: calls.append(("init", pk))
        or ({"total_summary": {"due": 0}}, "version", context),
    )
    monkeypatch.setattr(
        stripe,
        "fetch_elements_session",
        lambda _http, pk, *_args: calls.append(("elements", pk)) or {},
    )
    monkeypatch.setattr(stripe, "update_tax_region", lambda *_args: {})
    monkeypatch.setattr(stripe, "snapshot_billing", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        stripe,
        "confirm_payment",
        lambda _http, pk, *_args: calls.append(("confirm", pk))
        or {
            "next_action": {
                "redirect_to_url": {
                    "url": "https://pm-redirects.stripe.com/authorize/dynamic"
                }
            }
        },
    )

    redirect, returned_key, _context = stripe.stripe_to_paypal_redirect(
        object(),
        "cs_live_dynamic",
        billing={"name": "Owner", "address": {"country": "US"}},
        publishable_key="pk_live_dynamicShard123",
    )

    assert redirect.endswith("/dynamic")
    assert returned_key == "pk_live_dynamicShard123"
    assert calls == [
        ("init", "pk_live_dynamicShard123"),
        ("elements", "pk_live_dynamicShard123"),
        ("confirm", "pk_live_dynamicShard123"),
    ]


def test_gateway_creates_country_aligned_zero_oaics_checkout(monkeypatch):
    calls = []
    build_calls = []
    preflight_calls = []

    class Http:
        def close(self):
            calls.append("close")

    def build_http(proxy):
        build_calls.append(proxy)
        return Http()

    monkeypatch.setattr(stripe, "build_http", build_http)
    monkeypatch.setattr(
        gateway_module,
        "preflight_checkout_route",
        lambda **kwargs: preflight_calls.append(kwargs),
    )

    def create_order(*_args, **kwargs):
        assert kwargs["with_promo"] is True
        assert kwargs["country"] == "DE"
        assert kwargs["currency"] == "EUR"
        kwargs["checkout_context"].update(
            {
                "processor_entity": "openai_ie",
            }
        )
        return "oaics_dynamic", None

    monkeypatch.setattr(stripe, "create_chatgpt_order_with_retry", create_order)
    monkeypatch.setattr(
        stripe,
        "fetch_oaics_checkout_session",
        lambda *_args, **_kwargs: calls.append("fetch")
        or {
            "currency": "EUR",
            "total_summary": {"due": 0},
            "custom_payment_methods": [{"id": "cpmt_paypal", "type": "paypal"}],
        },
    )
    def taxes(*_args, **kwargs):
        calls.append(("taxes", kwargs["country"], kwargs["currency"], kwargs["billing"]))
        return {"currency": "EUR", "total_summary": {"due": 0}}

    monkeypatch.setattr(stripe, "submit_oaics_checkout_taxes", taxes)
    monkeypatch.setattr(
        stripe,
        "wait_for_oaics_zero",
        lambda *_args, **kwargs: calls.append(("zero", kwargs["country"], kwargs["currency"]))
        or kwargs["initial_payload"],
    )
    artifact = LiveProtocolGateway().create_checkout(
        access_token="test-at",
        proxy_country=get_country("BR"),
        checkout_country=get_country("DE"),
        billing={
            "name": "Owner",
            "email": "owner@example.com",
            "address": {"country": "DE", "line1": "Test", "city": "Berlin"},
        },
        proxy=parse_proxy_lines("checkout.example:1000")[0],
        device_id="device-test",
        log=lambda _message: None,
    )

    assert artifact.session_id == "oaics_dynamic"
    assert artifact.processor_entity == "openai_ie"
    assert artifact.country == "DE"
    assert artifact.currency == "EUR"
    assert build_calls == ["socks5://checkout.example:1000"]
    assert preflight_calls[0]["proxy_country"] == "BR"
    assert calls[0] == "fetch"
    assert calls[1][0:3] == ("taxes", "DE", "EUR")
    assert calls[1][3]["address"]["country"] == "DE"
    assert calls[2] == ("zero", "DE", "EUR")
    artifact.close_transport()


def test_preflight_rebuilds_session_once_after_transient_network_failure(monkeypatch):
    first_http = object()
    second_http = object()
    preflight_calls = []
    renew_calls = []
    logs = []

    def preflight(**kwargs):
        preflight_calls.append(kwargs["http"])
        if kwargs["http"] is first_http:
            raise stripe.CheckoutPreflightError(
                "temporary proxy interruption",
                code="PROXY_UNAVAILABLE",
            )

    def renew(current):
        renew_calls.append(current)
        return second_http

    monkeypatch.setattr(gateway_module, "preflight_checkout_route", preflight)

    result = gateway_module.preflight_checkout_route_with_retry(
        http=first_http,
        proxy_country="BR",
        access_token="test-at",
        device_id="device-test",
        log=logs.append,
        renew_http=renew,
    )

    assert result is second_http
    assert preflight_calls == [first_http, second_http]
    assert renew_calls == [first_http]
    assert logs == ["预检网络中断，重建代理连接后重试一次"]


def test_preflight_does_not_retry_country_mismatch(monkeypatch):
    renew_calls = []

    def preflight(**_kwargs):
        raise stripe.CheckoutPreflightError(
            "wrong country",
            code="PROXY_COUNTRY_MISMATCH",
        )

    monkeypatch.setattr(gateway_module, "preflight_checkout_route", preflight)

    with pytest.raises(stripe.CheckoutPreflightError) as captured:
        gateway_module.preflight_checkout_route_with_retry(
            http=object(),
            proxy_country="BR",
            access_token="test-at",
            device_id="device-test",
            log=lambda _message: None,
            renew_http=lambda current: renew_calls.append(current),
        )

    assert captured.value.code == "PROXY_COUNTRY_MISMATCH"
    assert renew_calls == []


def test_gateway_rejects_hosted_checkout_before_stripe_init(monkeypatch):
    class Http:
        def close(self):
            pass

    monkeypatch.setattr(stripe, "build_http", lambda _proxy: Http())
    monkeypatch.setattr(gateway_module, "preflight_checkout_route", lambda **_kwargs: None)
    monkeypatch.setattr(
        stripe,
        "create_chatgpt_order_with_retry",
        lambda *_args, **_kwargs: ("cs_live_wrong_kind", None),
    )
    monkeypatch.setattr(
        stripe,
        "verify_pk",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("OAICS gateway must not call Stripe init/key probing")
        ),
    )

    with pytest.raises(stripe.OaicsCheckoutRequiredError, match="普通 Stripe Checkout"):
        LiveProtocolGateway().create_checkout(
            access_token="test-at",
            proxy_country=get_country("BR"),
            checkout_country=get_country("DE"),
            billing={"name": "Owner", "email": "owner@example.com", "address": {"country": "DE"}},
            proxy=parse_proxy_lines("checkout.example:1000")[0],
            device_id="device-test",
            log=lambda _message: None,
        )


@pytest.mark.parametrize(
    "payload",
    (
        {},
        {"currency": "EUR", "total_summary": {"due": 1}},
        {
            "currency": "EUR",
            "total_summary": {"due": 0},
            "checkout_session": {"totalSummary": {"due": {"minorUnitsAmount": "5"}}},
        },
    ),
)
def test_oaics_zero_verification_rejects_unknown_or_any_nonzero_amount(payload):
    with pytest.raises(stripe.PromoNotAppliedError):
        stripe.verify_oaics_zero_snapshot(
            payload,
            session_id="oaics_amount_test",
            currency="EUR",
        )


def test_oaics_taxes_payload_follows_selected_country_and_currency():
    captured = {}

    class Response:
        status_code = 200
        text = '{"checkout_session":{"currency":"BRL","total_summary":{"due":0}}}'

        def json(self):
            return json.loads(self.text)

    class Http:
        def post(self, url, **kwargs):
            captured["url"] = url
            captured.update(kwargs)
            return Response()

    result = stripe.submit_oaics_checkout_taxes(
        Http(),
        "test-at",
        "oaics_country_test",
        "openai_llc",
        billing={
            "name": "Owner",
            "email": "owner@example.com",
            "address": {
                "country": "BR",
                "line1": "Avenida Paulista 1000",
                "city": "Sao Paulo",
                "state": "SP",
                "postal_code": "01310-100",
            },
        },
        country="BR",
        currency="BRL",
        device_id="device-test",
    )

    assert result["checkout_session"]["total_summary"]["due"] == 0
    assert captured["url"] == stripe.OPENAI_CHECKOUT_TAXES_URL
    assert captured["json"]["billing_country"] == "BR"
    assert captured["json"]["currency"] == "BRL"
    assert captured["json"]["billing_address"]["country"] == "BR"
    assert captured["json"]["billing_address"]["state"] == "SP"
    assert captured["headers"]["x-openai-target-route"] == (
        "/backend-api/payments/checkout/taxes"
    )


def test_oaics_paypal_flow_refreshes_sentinel_after_blocked(monkeypatch):
    events = []
    sentinel_pairs = iter((("sen-1", "so-1"), ("sen-2", "so-2")))

    class Response:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self.payload = payload
            self.text = json.dumps(payload)

        def json(self):
            return self.payload

    class Http:
        cookies = {}

        def get(self, url, **kwargs):
            events.append(("fetch", url, kwargs))
            return Response(
                200,
                {
                    "currency": "EUR",
                    "total_summary": {"due": 0},
                    "custom_payment_methods": [
                        {"id": "cpmt_other", "name": "Other"},
                        {"id": "cpmt_paypal", "name": "PayPal"},
                    ],
                },
            )

        def post(self, url, **kwargs):
            if url == stripe.OPENAI_CHECKOUT_CONFIRM_URL:
                confirmations = sum(1 for event in events if event[0] == "confirm")
                events.append(("confirm", url, kwargs))
                if confirmations == 0:
                    return Response(200, {"status": "blocked"})
                return Response(200, {"status": "success"})
            if url == stripe.OPENAI_CUSTOM_PAYMENT_START_URL:
                events.append(("start", url, kwargs))
                return Response(
                    200,
                    {
                        "status": "requires_action",
                        "next_action": {
                            "paymentMethodType": "paypal",
                            "url": (
                                "https://www.paypal.com/agreements/approve"
                                "?ba_token=BA-OAICS"
                            ),
                        },
                    },
                )
            raise AssertionError(f"unexpected POST {url}")

    def mint(flow, context, _log):
        assert flow == "checkout_session_approval"
        assert context.country == "DE"
        assert context.page_url.endswith("/openai_ie/oaics_paypal_test")
        return next(sentinel_pairs)

    monkeypatch.setattr(stripe, "_mint_sentinel", mint)
    logs = []
    redirect, context = stripe.oaics_to_paypal_redirect(
        Http(),
        "oaics_paypal_test",
        access_token="test-at",
        processor_entity="openai_ie",
        billing={
            "name": "Owner",
            "email": "owner@example.com",
            "address": {"country": "DE"},
        },
        country="DE",
        currency="EUR",
        device_id="device-test",
        sentinel_proxy="socks5://proxy.example:1000",
        log=logs.append,
    )

    assert redirect.endswith("BA-OAICS")
    assert context["custom_payment_method_id"] == "cpmt_paypal"
    assert [event[0] for event in events] == ["fetch", "confirm", "confirm", "start"]
    first_confirm = events[1][2]
    second_confirm = events[2][2]
    start = events[3][2]
    assert first_confirm["json"] == {
        "checkout_session_id": "oaics_paypal_test",
        "processor_entity": "openai_ie",
        "selected_payment_method_type": "cpmt_paypal",
    }
    assert first_confirm["headers"]["OpenAI-Sentinel-Token"] == "sen-1"
    assert second_confirm["headers"]["OpenAI-Sentinel-Token"] == "sen-2"
    assert start["json"]["processor_entity"] == "openai_ie"
    assert start["json"]["custom_payment_method_type_id"] == "cpmt_paypal"
    assert any("刷新 SEN/SO" in message for message in logs)
    diagnostics = [
        json.loads(message.removeprefix("[protocol-diagnostic] "))
        for message in logs
        if message.startswith("[protocol-diagnostic] ")
    ]
    assert [item["kind"] for item in diagnostics] == [
        "checkout_state",
        "checkout_confirm",
        "checkout_confirm",
        "custom_payment_start",
    ]
    assert diagnostics[1]["http_status"] == 200
    assert diagnostics[1]["response"]["status"] == "blocked"
    assert diagnostics[2]["response"]["status"] == "success"
    start_url = diagnostics[3]["response"]["next_action"]["url"]
    assert start_url.endswith("ba_token=%5BREDACTED%5D")
    assert "BA-OAICS" not in start_url


def test_oaics_standard_paypal_uses_payment_method_types_and_intent_confirm(
    monkeypatch,
):
    events = []

    class Response:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self.payload = payload
            self.text = json.dumps(payload)

        def json(self):
            return self.payload

    class Http:
        cookies = {}

        def get(self, url, **kwargs):
            if url.startswith(f"{stripe.OPENAI_CHECKOUT_URL}/"):
                events.append(("checkout", url, kwargs))
                return Response(
                    200,
                    {
                        "checkout_session_id": "oaics_standard_paypal",
                        "checkout_provider": "open_ai",
                        "publishable_key": "pk_live_standard",
                        "customer_session_client_secret": "cuss_live_standard",
                        "currency": "eur",
                        "total_summary": {"due": 0},
                        "payment_method_types": ["link", "paypal", "card"],
                        "custom_payment_methods": [],
                    },
                )
            if url == f"{stripe.STRIPE_API}/v1/elements/sessions":
                events.append(("elements", url, kwargs))
                return Response(
                    200,
                    {
                        "id": "elements_session_standard",
                        "config_id": "elements_config_standard",
                        "customer": {
                            "customer_session": {
                                "api_key": "ek_live_standard",
                                "customer": "cus_standard",
                            }
                        },
                    },
                )
            raise AssertionError(f"unexpected GET {url}")

        def post(self, url, **kwargs):
            if url == f"{stripe.STRIPE_API}/v1/confirmation_tokens":
                events.append(("confirmation_token", url, kwargs))
                return Response(200, {"id": "ctoken_standard"})
            if url == stripe.OPENAI_CHECKOUT_CONFIRM_URL:
                events.append(("checkout_confirm", url, kwargs))
                return Response(
                    200,
                    {
                        "status": "success",
                        "type": "setup_intent",
                        "client_secret": "seti_standard_secret_value",
                        "confirm_return_url": (
                            "https://chatgpt.com/checkout/verify"
                            "?stripe_session_id=oaics_standard_paypal"
                        ),
                    },
                )
            if url == f"{stripe.STRIPE_API}/v1/setup_intents/seti_standard/confirm":
                events.append(("intent_confirm", url, kwargs))
                return Response(
                    200,
                    {
                        "status": "requires_action",
                        "next_action": {
                            "redirect_to_url": {
                                "url": (
                                    "https://pm-redirects.stripe.com/authorize/standard"
                                )
                            }
                        },
                    },
                )
            if url == stripe.OPENAI_CUSTOM_PAYMENT_START_URL:
                raise AssertionError("standard PayPal must not use custom payment endpoint")
            raise AssertionError(f"unexpected POST {url}")

    monkeypatch.setattr(stripe, "_mint_sentinel", lambda *_args: ("sen", "so"))
    redirect, context = stripe.oaics_to_paypal_redirect(
        Http(),
        "oaics_standard_paypal",
        access_token="test-at",
        processor_entity="openai_ie",
        billing={
            "name": "Owner",
            "email": "owner@example.com",
            "address": {
                "country": "DE",
                "line1": "Teststrasse 1",
                "city": "Berlin",
                "postal_code": "10115",
            },
        },
        country="DE",
        currency="EUR",
        device_id="device-test",
        sentinel_proxy="socks5://proxy.example:1000",
    )

    assert redirect == "https://pm-redirects.stripe.com/authorize/standard"
    assert context["selected_payment_method_type"] == "paypal"
    assert [event[0] for event in events] == [
        "checkout",
        "elements",
        "confirmation_token",
        "checkout_confirm",
        "intent_confirm",
    ]
    elements_request = events[1][2]
    assert elements_request["params"]["deferred_intent[payment_method_types][1]"] == "paypal"
    token_request = events[2][2]
    assert token_request["headers"]["Authorization"] == "Bearer pk_live_standard"
    assert token_request["data"]["payment_method_data[type]"] == "paypal"
    assert token_request["data"]["payment_method_data[billing_details][address][country]"] == "DE"
    assert token_request["data"]["mandate_data[customer_acceptance][type]"] == "online"
    assert (
        token_request["data"][
            "mandate_data[customer_acceptance][online][infer_from_client]"
        ]
        == "true"
    )
    checkout_confirm = events[3][2]
    assert checkout_confirm["json"] == {
        "checkout_session_id": "oaics_standard_paypal",
        "confirm_token": "ctoken_standard",
        "selected_payment_method_type": "paypal",
    }
    intent_confirm = events[4][2]
    assert intent_confirm["data"]["confirmation_token"] == "ctoken_standard"
    assert intent_confirm["data"]["client_secret"] == "seti_standard_secret_value"
    assert intent_confirm["data"]["use_stripe_sdk"] == "true"
    assert not any(key.startswith("mandate_data[") for key in intent_confirm["data"])


def test_oaics_explicit_card_only_state_is_not_reported_as_custom_paypal(
    monkeypatch,
):
    state = {
        "currency": "eur",
        "total_summary": {"due": 0},
        "payment_method_types": ["link", "card"],
        "custom_payment_methods": [],
    }
    monkeypatch.setattr(
        stripe,
        "fetch_oaics_checkout_session",
        lambda *_args, **_kwargs: state,
    )

    with pytest.raises(stripe.PayPalFundingUnavailableError) as exc_info:
        stripe.oaics_to_paypal_redirect(
            object(),
            "oaics_card_only",
            access_token="test-at",
            processor_entity="openai_ie",
            billing={"address": {"country": "DE"}},
            country="DE",
            currency="EUR",
            device_id="device-test",
            sentinel_proxy="socks5://proxy.example:1000",
        )

    assert exc_info.value.payment_method_types == ["link", "card"]
    assert "未包含 paypal" in str(exc_info.value)


def test_oaics_confirmation_token_surfaces_stripe_error_code():
    class Response:
        status_code = 403
        text = json.dumps(
            {
                "error": {
                    "code": "more_permissions_required",
                    "message": "details omitted",
                }
            }
        )

        def json(self):
            return json.loads(self.text)

    class Http:
        def post(self, *_args, **_kwargs):
            return Response()

    with pytest.raises(RuntimeError, match="403 \\(more_permissions_required\\)"):
        stripe.create_oaics_paypal_confirmation_token(
            Http(),
            {},
            {
                "_oaics_publishable_key": "pk_live_standard",
                "_oaics_stripe_js_id": "stripe-js-standard",
                "_oaics_payment_method_types": ["paypal"],
                "id": "elements_session_standard",
                "config_id": "elements_config_standard",
            },
            billing={"address": {"country": "DE"}},
            currency="EUR",
        )


@pytest.mark.parametrize(
    ("init_data", "context"),
    (
        ({"total_summary": {"due": 2000}}, {"checkout_amount": 2000}),
        ({}, {}),
        ({"total_summary": {"due": "invalid"}}, {}),
    ),
)
def test_nonzero_or_unverifiable_checkout_stops_before_paypal(
    monkeypatch, init_data, context
):
    calls = []
    monkeypatch.setattr(stripe, "verify_pk", lambda *_args: "pk_test")
    monkeypatch.setattr(
        stripe,
        "init_checkout",
        lambda *_args: (init_data, "version", context),
    )
    monkeypatch.setattr(
        stripe,
        "fetch_elements_session",
        lambda *_args: calls.append("elements") or {},
    )
    monkeypatch.setattr(
        stripe,
        "confirm_payment",
        lambda *_args: calls.append("confirm") or {},
    )

    with pytest.raises(stripe.PromoNotAppliedError, match="Checkout due="):
        stripe.stripe_to_paypal_redirect(
            object(),
            "cs_live_not_free",
            billing={"name": "Owner", "address": {"country": "US"}},
            require_zero_amount=True,
        )

    assert calls == []


def test_explicit_card_only_checkout_stops_before_confirm_or_approve(monkeypatch):
    calls = []
    context = {
        "payment_method_types": ["card"],
        "checkout_amount": 0,
        "currency": "eur",
    }
    monkeypatch.setattr(stripe, "verify_pk", lambda *_args: "pk_test")
    monkeypatch.setattr(
        stripe,
        "init_checkout",
        lambda *_args: (
            {"total_summary": {"due": 0}, "payment_method_types": ["card"]},
            "version",
            context,
        ),
    )
    monkeypatch.setattr(stripe, "fetch_elements_session", lambda *_args: {})
    monkeypatch.setattr(
        stripe,
        "confirm_payment",
        lambda *_args: (_ for _ in ()).throw(AssertionError("confirm must not run")),
    )
    monkeypatch.setattr(
        stripe,
        "approve_submission",
        lambda *_args, **_kwargs: calls.append("approve") or {"result": "approved"},
    )
    with pytest.raises(stripe.PayPalFundingUnavailableError, match="未提供 PayPal"):
        stripe.stripe_to_paypal_redirect(
            object(),
            "cs_live_test",
            billing={"name": "Owner", "email": "owner@example.com", "address": {"country": "DE"}},
            country="DE",
            processor_entity="openai_ie",
            chatgpt_http=object(),
            access_token="test-at",
        )
    assert calls == []


def test_approve_blocked_raises_immediately(monkeypatch):
    """When approve returns blocked, raise immediately without polling."""
    calls = []
    context = {"payment_method_types": ["paypal"], "checkout_amount": 0, "currency": "eur"}
    monkeypatch.setattr(stripe, "verify_pk", lambda *_args: "pk_test")
    monkeypatch.setattr(stripe, "init_checkout", lambda *_args: ({"total_summary": {"due": 0}}, "version", context))
    monkeypatch.setattr(stripe, "fetch_elements_session", lambda *_args: {})
    monkeypatch.setattr(
        stripe,
        "confirm_payment",
        lambda *_args: {"submission_attempt": {"state": "requires_approval"}},
    )
    monkeypatch.setattr(
        stripe,
        "approve_submission",
        lambda *_args, **_kwargs: calls.append("approve") or {"result": "blocked"},
    )
    monkeypatch.setattr(
        stripe,
        "poll_redirect_after_approve",
        lambda *_args, **_kwargs: calls.append("poll") or "",
    )
    with pytest.raises(RuntimeError, match="blocked"):
        stripe.stripe_to_paypal_redirect(
            object(),
            "cs_live_test",
            billing={"name": "Owner", "email": "owner@example.com", "address": {"country": "DE"}},
            country="DE",
            processor_entity="openai_ie",
            chatgpt_http=object(),
            access_token="test-at",
        )
    # Should NOT have polled — blocked means immediate failure
    assert calls == ["approve"]


def test_approve_blocked_error_message_contains_blocked(monkeypatch):
    """Verify the error message is descriptive when approve is blocked."""
    context = {"payment_method_types": ["paypal"], "checkout_amount": 0, "currency": "eur"}
    monkeypatch.setattr(stripe, "verify_pk", lambda *_args: "pk_test")
    monkeypatch.setattr(stripe, "init_checkout", lambda *_args: ({"total_summary": {"due": 0}}, "version", context))
    monkeypatch.setattr(stripe, "fetch_elements_session", lambda *_args: {})
    monkeypatch.setattr(
        stripe,
        "confirm_payment",
        lambda *_args: {"submission_attempt": {"state": "requires_approval"}},
    )
    monkeypatch.setattr(
        stripe,
        "approve_submission",
        lambda *_args, **_kwargs: {"result": "blocked"},
    )
    with pytest.raises(RuntimeError, match="blocked"):
        stripe.stripe_to_paypal_redirect(
            object(),
            "cs_live_test",
            billing={"name": "Owner", "email": "owner@example.com", "address": {"country": "DE"}},
            country="DE",
            processor_entity="openai_ie",
            chatgpt_http=object(),
            access_token="test-at",
        )


def test_approval_sentinel_is_prefetched_and_reconfirm_runs_before_poll(monkeypatch):
    calls = []
    context = {"payment_method_types": ["paypal"], "checkout_amount": 0, "currency": "eur"}
    monkeypatch.setattr(stripe, "verify_pk", lambda *_args: "pk_test")
    monkeypatch.setattr(
        stripe,
        "init_checkout",
        lambda *_args: calls.append("init")
        or (
            {"total_summary": {"due": 0}, "payment_method_types": ["paypal"]},
            "version",
            context,
        ),
    )
    monkeypatch.setattr(
        stripe,
        "fetch_elements_session",
        lambda *_args: calls.append("elements")
        or {"payment_method_specs": [{"type": "paypal"}]},
    )
    monkeypatch.setattr(stripe, "update_tax_region", lambda *_args: calls.append("tax"))
    monkeypatch.setattr(
        stripe,
        "snapshot_billing",
        lambda *_args, **_kwargs: calls.append("snapshot"),
    )
    monkeypatch.setattr(
        stripe,
        "prepare_approve_sentinel_headers",
        lambda *_args, **_kwargs: calls.append("prepare")
        or {"OpenAI-Sentinel-Token": "prefetched", "OAI-Telemetry": "[1,null]"},
    )
    confirms = [
        {"submission_attempt": {"state": "requires_approval"}},
        {
            "next_action": {
                "redirect_to_url": {
                    "url": "https://pm-redirects.stripe.com/authorize/reconfirm"
                }
            }
        },
    ]

    def confirm(*_args):
        calls.append("confirm")
        return confirms.pop(0)

    monkeypatch.setattr(stripe, "confirm_payment", confirm)

    def approve(*_args, **kwargs):
        calls.append("approve")
        assert kwargs["prepared_headers"]["OpenAI-Sentinel-Token"] == "prefetched"
        return {"result": "approved"}

    monkeypatch.setattr(stripe, "approve_submission", approve)
    monkeypatch.setattr(
        stripe,
        "poll_redirect_after_approve",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("poll must not run before successful re-confirm")
        ),
    )

    redirect, _pk, _ctx = stripe.stripe_to_paypal_redirect(
        object(),
        "cs_live_test",
        billing={"name": "Owner", "email": "owner@example.com", "address": {"country": "DE"}},
        country="DE",
        processor_entity="openai_ie",
        chatgpt_http=object(),
        access_token="test-at",
        device_id="device-test",
        sentinel_proxy="socks5h://proxy.test:1080",
    )

    assert redirect.endswith("/reconfirm")
    assert calls == [
        "init",
        "elements",
        "tax",
        "snapshot",
        "prepare",
        "confirm",
        "approve",
        "confirm",
    ]


def test_approve_uses_telemetry_and_session_aligned_sentinel_context(monkeypatch):
    captured = {}

    class Response:
        status_code = 200
        text = '{"result":"approved"}'

        def json(self):
            return {"result": "approved"}

    class Http:
        def post(self, _url, **kwargs):
            captured["headers"] = kwargs["headers"]
            return Response()

    monkeypatch.setattr(
        stripe,
        "_warmup_chatgpt_page",
        lambda *_args, **kwargs: captured.setdefault("page_url", kwargs["page_url"]),
    )
    monkeypatch.setattr(
        stripe,
        "_session_cookie_header",
        lambda _http: "oai-did=device-test; __cf_bm=cf-test",
    )

    def mint(flow, context, _log):
        captured["flow"] = flow
        captured["context"] = context
        return "sentinel-main", "sentinel-so"

    monkeypatch.setattr(stripe, "_mint_sentinel", mint)
    result = stripe.approve_submission(
        Http(),
        "test-at",
        "cs_live_test",
        "openai_llc",
        lambda _message: None,
        device_id="device-test",
        sentinel_proxy="socks5h://proxy.test:1080",
        country="US",
    )

    assert result == {"result": "approved"}
    assert captured["page_url"].endswith("/openai_llc/cs_live_test")
    assert captured["flow"] == "checkout_session_approval"
    assert captured["context"].device_id == "device-test"
    assert captured["context"].country == "US"
    assert captured["context"].page_url == captured["page_url"]
    assert "__cf_bm=cf-test" in captured["context"].cookie_header
    assert captured["headers"]["OAI-Telemetry"] == "[1,null]"
    assert captured["headers"]["OpenAI-Sentinel-Token"] == "sentinel-main"
    assert captured["headers"]["OpenAI-Sentinel-So-Token"] == "sentinel-so"


def test_approve_poll_stops_immediately_on_current_paypal_generic_decline(monkeypatch):
    calls = []

    class Response:
        status_code = 200

        def json(self):
            return {
                "setup_intent": {
                    "last_setup_error": {
                        "code": "setup_attempt_failed",
                        "decline_code": "generic_decline",
                        "payment_method": {"id": "pm_current", "type": "paypal"},
                    }
                }
            }

    class Http:
        def get(self, *_args, **_kwargs):
            calls.append("get")
            return Response()

    monkeypatch.setattr(stripe.time, "sleep", lambda _seconds: None)
    with pytest.raises(stripe.PayPalRiskDeclinedError, match="generic_decline"):
        stripe.poll_redirect_after_approve(
            Http(),
            "pk_test",
            "cs_live_test",
            lambda _message: None,
            payment_method_id="pm_current",
        )
    assert calls == ["get"]


def test_stale_generic_decline_from_previous_payment_method_is_ignored(monkeypatch):
    class Response:
        status_code = 200

        def json(self):
            return {
                "submission_attempt": {"state": "requires_approval"},
                "setup_intent": {
                    "last_setup_error": {
                        "code": "setup_attempt_failed",
                        "decline_code": "generic_decline",
                        "payment_method": {"id": "pm_previous", "type": "paypal"},
                    }
                },
            }

    class Http:
        def get(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(stripe.time, "sleep", lambda _seconds: None)
    assert (
        stripe.poll_redirect_after_approve(
            Http(),
            "pk_test",
            "cs_live_test",
            lambda _message: None,
            max_attempts=1,
            payment_method_id="pm_current",
        )
        == ""
    )


def test_paypal_confirm_logs_complete_redacted_response(monkeypatch):
    context = {"payment_method_types": ["paypal"], "checkout_amount": 0, "currency": "eur"}
    confirm_data = {
        "id": "ppage_test",
        "object": "checkout.session",
        "account": "acct_new_shard",
        "client_secret": "seti_secret_value",
        "billing_details": {"email": "owner@example.com", "name": "Owner"},
        "diagnostic_padding": "x" * 1200,
        "next_action": {
            "redirect_to_url": {"url": "https://pm-redirects.stripe.com/authorize/test"}
        },
    }
    monkeypatch.setattr(stripe, "verify_pk", lambda *_args: "pk_test")
    monkeypatch.setattr(
        stripe,
        "init_checkout",
        lambda *_args: ({"total_summary": {"due": 0}}, "version", context),
    )
    monkeypatch.setattr(stripe, "fetch_elements_session", lambda *_args: {})
    monkeypatch.setattr(stripe, "create_paypal_payment_method", lambda *_args: "pm_test")
    monkeypatch.setattr(stripe, "confirm_payment", lambda *_args: confirm_data)
    logs = []
    redirect, _pk, _context = stripe.stripe_to_paypal_redirect(
        object(),
        "cs_live_test",
        billing={"name": "Owner", "email": "owner@example.com", "address": {"country": "DE"}},
        country="DE",
        processor_entity="openai_ie",
        access_token="test-at",
        log=logs.append,
    )
    diagnostic = next(item for item in logs if "confirm 完整响应（已脱敏）" in item)
    assert redirect.endswith("/test")
    assert len(diagnostic) > 1200
    assert "ppage_test" in diagnostic
    assert "acct_new_shard" in diagnostic
    assert "x" * 1200 in diagnostic
    assert "seti_secret_value" not in diagnostic
    assert "owner@example.com" not in diagnostic


def test_resolves_pm_redirect_to_paypal_ba_url():
    responses = [
        SimpleNamespace(headers={"location": "https://www.paypal.com/agreements/approve?ba_token=BA-ABC123"}, text="")
    ]

    class Http:
        def get(self, *_args, **_kwargs):
            return responses.pop(0)

    approval, token = resolve_paypal_approval_url(
        Http(), "https://pm-redirects.stripe.com/authorize/test"
    )
    assert token == "BA-ABC123"
    assert approval.endswith("ba_token=BA-ABC123")


def test_paypal_confirm_uses_inline_payment_method_data():
    captured = []

    class Response:
        status_code = 200
        text = ""

        def json(self):
            return {"next_action": {}}

    class Http:
        def post(self, url, **kwargs):
            captured.append((url, kwargs["data"]))
            return Response()

    billing = {
        "name": "Owner",
        "email": "owner@example.com",
        "address": {
            "country": "DE",
            "line1": "Friedrichstrasse 10",
            "city": "Berlin",
            "postal_code": "10117",
            "state": "BE",
        },
    }
    base_context = {
        "billing": billing,
        "checkout_amount": 0,
        "currency": "eur",
        "stripe_js_id": "stripe-js",
        "client_session_id": "client-session",
        "elements_session_id": "elements-session",
        "elements_session_config_id": "elements-config",
        "config_id": "checkout-config",
        "processor_entity": "openai_ie",
        "stripe_hosted_url": "https://checkout.stripe.com/c/pay/cs_test",
        "return_url": (
            "https://chatgpt.com/checkout/verify?stripe_session_id=cs_test"
            "&processor_entity=openai_ie&plan_type=plus"
        ),
        "guid": "guid",
        "muid": "muid",
        "sid": "sid",
    }
    init_data = {"total_summary": {"due": 0}}

    stripe.confirm_payment(
        Http(),
        "pk_test",
        "cs_test",
        "pm_created_but_not_referenced",
        init_data,
        "version",
        dict(base_context),
        stripe._profile("DE"),
        lambda _message: None,
    )
    paypal_data = captured[-1][1]
    assert paypal_data["eid"] == "NA"
    assert paypal_data["client_attribution_metadata[client_session_id]"] == "client-session"
    assert paypal_data["client_attribution_metadata[merchant_integration_version]"] == "custom_checkout"
    assert paypal_data["link_brand"] == "link"
    assert paypal_data["consent[terms_of_service]"] == "accepted"
    assert paypal_data["return_url"].startswith("https://pay.openai.com/c/pay/cs_test?")
    assert "success_return_url=" in paypal_data["return_url"]
    assert paypal_data["payment_method_data[type]"] == "paypal"
    assert paypal_data["payment_method_data[billing_details][address][country]"] == "DE"
    assert (
        paypal_data[
            "payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][0]"
        ]
        == "expressCheckout"
    )
    assert "payment_method" not in paypal_data

def test_paypal_stripe_flow_updates_tax_and_snapshot_before_confirm(monkeypatch):
    calls = []
    context = {"payment_method_types": ["paypal"], "checkout_amount": 0, "currency": "eur"}
    monkeypatch.setattr(stripe, "verify_pk", lambda *_args: calls.append("verify") or "pk_test")
    monkeypatch.setattr(
        stripe,
        "init_checkout",
        lambda *_args: calls.append("init")
        or ({"total_summary": {"due": 0}}, "version", context),
    )
    monkeypatch.setattr(
        stripe,
        "fetch_elements_session",
        lambda *_args: calls.append("elements") or {},
    )
    monkeypatch.setattr(
        stripe,
        "update_tax_region",
        lambda *_args: calls.append("tax") or {},
    )
    monkeypatch.setattr(
        stripe,
        "snapshot_billing",
        lambda *_args, **_kwargs: calls.append("snapshot"),
    )
    monkeypatch.setattr(
        stripe,
        "confirm_payment",
        lambda *_args: calls.append("confirm")
        or {
            "next_action": {
                "redirect_to_url": {
                    "url": "https://pm-redirects.stripe.com/authorize/test"
                }
            }
        },
    )

    redirect, _pk, _context = stripe.stripe_to_paypal_redirect(
        object(),
        "cs_test",
        billing={"name": "Owner", "address": {"country": "DE"}},
        country="DE",
        processor_entity="openai_ie",
        require_zero_amount=True,
        chatgpt_http=object(),
        access_token="test-at",
    )

    assert redirect.endswith("/test")
    assert calls == [
        "verify",
        "init",
        "elements",
        "tax",
        "snapshot",
        "confirm",
    ]


def test_gateway_stops_after_extracting_paypal_ba_link(monkeypatch):
    calls = []
    verified_countries = []

    class Http:
        def close(self):
            calls.append("close")

    http = Http()
    monkeypatch.setattr(stripe, "build_http", lambda _proxy: calls.append("build") or http)
    monkeypatch.setattr(
        stripe,
        "verify_proxy_exit_country",
        lambda _http, expected_country, **_kwargs: verified_countries.append(expected_country) or {
            "ip": "203.0.113.10",
            "country": expected_country,
            "source": "test",
        },
    )
    monkeypatch.setattr(stripe, "verify_chatgpt_account", lambda *_args, **_kwargs: {})
    def oaics_confirm(*_args, **kwargs):
        assert kwargs["processor_entity"] == "openai_ie"
        assert kwargs["country"] == "DE"
        assert kwargs["currency"] == "EUR"
        calls.append("oaics_confirm")
        return (
            "https://www.paypal.com/agreements/approve?ba_token=BA-TEST",
            {"custom_payment_method_id": "cpmt_paypal"},
        )

    monkeypatch.setattr(stripe, "oaics_to_paypal_redirect", oaics_confirm)
    monkeypatch.setattr(
        gateway_module,
        "resolve_paypal_approval_url",
        lambda *_args, **_kwargs: calls.append("resolve_ba")
        or (
            "https://www.paypal.com/agreements/approve?ba_token=BA-TEST",
            "BA-TEST",
        ),
    )

    result = LiveProtocolGateway().attempt_provider(
        artifact=CheckoutArtifact(
            session_id="oaics_test",
            processor_entity="openai_ie",
            country="DE",
            currency="EUR",
            checkout_url="https://chatgpt.com/checkout/openai_ie/oaics_test",
            amount=0,
        ),
        access_token="test-at",
        proxy_country=get_country("BR"),
        checkout_country=get_country("DE"),
        billing={"name": "Owner", "address": {"country": "DE"}},
        proxy=parse_proxy_lines("proxy.example:1000")[0],
        device_id="device",
        log=lambda _message: None,
    )

    assert calls == [
        "build",
        "oaics_confirm",
        "resolve_ba",
        "close",
    ]
    assert verified_countries == ["BR"]
    assert result.paypal_approve_url.endswith("BA-TEST")
    assert result.provider_redirect_url.endswith("BA-TEST")
    assert result.ba_token == "BA-TEST"
