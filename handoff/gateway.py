from __future__ import annotations

import html
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import parse_qs, urljoin, urlsplit

from .countries import CountryProfile, install_protocol_profiles
from .protocol import stripe_checkout as stripe
from .proxies import ProxyEndpoint


install_protocol_profiles(stripe)

LogFn = Callable[[str], None]
_PAYPAL_URL_RE = re.compile(
    r"https?://(?:www\.)?paypal\.com/agreements/approve\?[^\s<>\"']+",
    re.IGNORECASE,
)
_BA_TOKEN_RE = re.compile(r"\bBA-[A-Z0-9-]+\b", re.IGNORECASE)


@dataclass(slots=True)
class CheckoutTransport:
    http: Any
    proxy_url: str
    claimed: bool = False

    def claim(self, proxy_url: str):
        if self.claimed or str(proxy_url or "") != self.proxy_url:
            return None
        self.claimed = True
        return self.http

    def close(self) -> None:
        self.claimed = True
        try:
            self.http.close()
        except Exception:
            pass


@dataclass(frozen=True, slots=True)
class CheckoutArtifact:
    session_id: str
    processor_entity: str
    country: str
    currency: str
    checkout_url: str
    amount: int | None = None
    publishable_key: str = field(default="", repr=False)
    transport: CheckoutTransport | None = field(default=None, repr=False, compare=False)

    def close_transport(self) -> None:
        if self.transport is not None:
            self.transport.close()


@dataclass(frozen=True, slots=True)
class ProviderResult:
    provider_redirect_url: str
    paypal_approve_url: str
    ba_token: str


def new_device_id() -> str:
    return str(uuid.uuid4())


def preflight_checkout_route(
    *,
    http,
    proxy_country: str,
    access_token: str,
    device_id: str,
    log: LogFn,
) -> None:
    checkout_info = stripe.verify_proxy_exit_country(http, proxy_country)
    log(f"Checkout 出口预检通过：{stripe.proxy_exit_log_label(checkout_info)}")
    stripe.verify_chatgpt_account(
        http,
        access_token,
        country=proxy_country,
        device_id=device_id,
    )
    log("ChatGPT /me 账号与连接预检通过")


def preflight_checkout_route_with_retry(
    *,
    http,
    proxy_country: str,
    access_token: str,
    device_id: str,
    log: LogFn,
    renew_http: Callable[[Any], Any],
):
    try:
        preflight_checkout_route(
            http=http,
            proxy_country=proxy_country,
            access_token=access_token,
            device_id=device_id,
            log=log,
        )
        return http
    except stripe.CheckoutPreflightError as exc:
        if exc.code not in {"PROXY_UNAVAILABLE", "CHATGPT_CONNECTION_FAILED"}:
            raise
        log("预检网络中断，重建代理连接后重试一次")
        replacement = renew_http(http)
        preflight_checkout_route(
            http=replacement,
            proxy_country=proxy_country,
            access_token=access_token,
            device_id=device_id,
            log=log,
        )
        return replacement


def _ba_from_url(url: str) -> str:
    try:
        token = (parse_qs(urlsplit(url).query).get("ba_token") or [""])[0]
    except Exception:
        token = ""
    if token:
        return token
    match = _BA_TOKEN_RE.search(url)
    return match.group(0) if match else ""


def resolve_paypal_approval_url(
    http,
    redirect_url: str,
    *,
    max_hops: int = 6,
    log: LogFn = lambda _message: None,
) -> tuple[str, str]:
    current = html.unescape(str(redirect_url or "").strip())
    if not current:
        raise RuntimeError("OAICS 未返回 PayPal 跳转地址")

    for _hop in range(max(1, max_hops) + 1):
        if "paypal.com/agreements/approve" in current.lower():
            token = _ba_from_url(current)
            if token:
                stripe._protocol_diagnostic(
                    log,
                    kind="paypal_redirect",
                    method="GET",
                    route="paypal/agreements/approve",
                    request_payload={"hop": _hop, "url": current},
                    response_payload={"ba_token_found": True},
                )
                return current, token
        response = http.get(
            current,
            allow_redirects=False,
            headers={
                "User-Agent": stripe.CHROME_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            timeout=30,
        )
        location = str((getattr(response, "headers", {}) or {}).get("location") or "")
        body = html.unescape(str(getattr(response, "text", "") or ""))
        match = _PAYPAL_URL_RE.search(body)
        stripe._protocol_diagnostic(
            log,
            kind="paypal_redirect",
            method="GET",
            route=urlsplit(current).path or "/",
            status=getattr(response, "status_code", 0),
            request_payload={"hop": _hop, "url": current},
            response_payload={
                "location": location,
                "paypal_url_in_body": bool(match),
                "ba_token_in_body": bool(_BA_TOKEN_RE.search(body)),
            },
            response=response,
        )
        if match:
            approval = match.group(0).replace("\\u0026", "&").replace("\\/", "/")
            token = _ba_from_url(approval)
            if token:
                return approval, token
        token_match = _BA_TOKEN_RE.search(body)
        if token_match and "paypal" in body.lower():
            token = token_match.group(0)
            return f"https://www.paypal.com/agreements/approve?ba_token={token}", token
        if not location:
            break
        current = urljoin(current, html.unescape(location))

    raise RuntimeError("未能从 OAICS 跳转解析 PayPal BA 链接")


class LiveProtocolGateway:
    def create_checkout(
        self,
        *,
        access_token: str,
        proxy_country: CountryProfile,
        checkout_country: CountryProfile,
        billing: dict,
        proxy: ProxyEndpoint,
        device_id: str,
        log: LogFn,
    ) -> CheckoutArtifact:
        http = stripe.build_http(proxy.url)
        keep_checkout_http = False
        context: dict[str, str] = {}

        def renew_checkout_http(current):
            nonlocal http
            http = stripe.renew_http_session(current, proxy.url)
            return http

        try:
            http = preflight_checkout_route_with_retry(
                http=http,
                proxy_country=proxy_country.code,
                access_token=access_token,
                device_id=device_id,
                log=log,
                renew_http=renew_checkout_http,
            )
            session_id, error = stripe.create_chatgpt_order_with_retry(
                http,
                access_token,
                country=checkout_country.code,
                currency=checkout_country.currency,
                device_id=device_id,
                sentinel_proxy=proxy.url,
                checkout_context=context,
                with_promo=True,
                max_attempts=3,
                renew_http=renew_checkout_http,
                log=log,
            )
            if not session_id:
                raise RuntimeError(f"创建 Checkout 失败: {error or '没有 session id'}")
            if not session_id.startswith("oaics_"):
                raise stripe.OaicsCheckoutRequiredError(session_id)
            processor_entity = (
                context.get("processor_entity") or checkout_country.processor_entity
            )
            state = stripe.fetch_oaics_checkout_session(
                http,
                access_token,
                session_id,
                processor_entity,
                country=checkout_country.code,
                device_id=device_id,
                log=log,
            )
            tax_state = stripe.submit_oaics_checkout_taxes(
                http,
                access_token,
                session_id,
                processor_entity,
                billing=billing,
                country=checkout_country.code,
                currency=checkout_country.currency,
                device_id=device_id,
                log=log,
            )
            state = stripe.wait_for_oaics_zero(
                http,
                access_token,
                session_id,
                processor_entity,
                country=checkout_country.code,
                currency=checkout_country.currency,
                device_id=device_id,
                initial_payload=tax_state or state,
                log=log,
            )
            detected_currency = stripe.oaics_checkout_currency(state)
            if detected_currency and detected_currency != checkout_country.currency:
                raise RuntimeError(
                    "OAICS 币种与账单国家不一致: "
                    f"expected={checkout_country.currency}, actual={detected_currency}"
                )
            canonical_checkout_url = (
                f"https://chatgpt.com/checkout/{processor_entity}/{session_id}"
            )
            response_checkout_url = context.get("checkout_url") or ""
            checkout_url = (
                response_checkout_url
                if session_id in response_checkout_url
                else canonical_checkout_url
            )
            transport = CheckoutTransport(http=http, proxy_url=proxy.url)
            keep_checkout_http = True
            return CheckoutArtifact(
                session_id=session_id,
                processor_entity=processor_entity,
                country=checkout_country.code,
                currency=detected_currency or checkout_country.currency,
                checkout_url=checkout_url,
                amount=0,
                transport=transport,
            )
        finally:
            if not keep_checkout_http:
                try:
                    http.close()
                except Exception:
                    pass

    def attempt_provider(
        self,
        *,
        artifact: CheckoutArtifact,
        access_token: str,
        proxy_country: CountryProfile,
        checkout_country: CountryProfile,
        billing: dict,
        proxy: ProxyEndpoint,
        device_id: str,
        check_cancelled: Callable[[], None] = lambda: None,
        log: LogFn,
    ) -> ProviderResult:
        http = artifact.transport.claim(proxy.url) if artifact.transport is not None else None
        reused_checkout_transport = http is not None
        if http is None:
            http = stripe.build_http(proxy.url)
        try:
            check_cancelled()
            if not reused_checkout_transport:
                info = stripe.verify_proxy_exit_country(http, proxy_country.code)
                log(f"提链出口预检通过：{stripe.proxy_exit_log_label(info)}")
                stripe.verify_chatgpt_account(
                    http,
                    access_token,
                    country=proxy_country.code,
                    device_id=device_id,
                )
                log("提链出口 ChatGPT /me 预检通过")
            else:
                log("首次提链复用 Checkout 会话")
            redirect_url, _context = stripe.oaics_to_paypal_redirect(
                http,
                artifact.session_id,
                access_token=access_token,
                processor_entity=artifact.processor_entity,
                billing=billing,
                country=checkout_country.code,
                currency=artifact.currency,
                device_id=device_id,
                sentinel_proxy=proxy.url,
                log=lambda raw: log(raw.removeprefix("[oaics] ")),
            )
            check_cancelled()
            approval_url, ba_token = resolve_paypal_approval_url(
                http,
                redirect_url,
                log=log,
            )
            return ProviderResult(
                provider_redirect_url=redirect_url,
                paypal_approve_url=approval_url,
                ba_token=ba_token,
            )
        finally:
            try:
                http.close()
            except Exception:
                pass
