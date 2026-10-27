"""Owner consent plus SDK OAuth 2.1/PKCE; Kalshi credentials never enter OAuth."""
import hashlib
import hmac
import secrets
import time
from urllib.parse import urlencode, urlsplit

from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams,
                                      AuthorizeError, RefreshToken, RegistrationError, TokenError)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken


def token_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


class OwnerOAuth:
    def __init__(self, settings, db):
        self.settings, self.db = settings, db

    def static_role(self, token):
        if hmac.compare_digest(token.encode(), self.settings.mcp_director_token.get_secret_value().encode()):
            return "trade"
        if hmac.compare_digest(token.encode(), self.settings.mcp_read_token.get_secret_value().encode()):
            return "read"
        return None

    def is_owner(self, token):
        return hmac.compare_digest(token.encode(), self.settings.admin_token.get_secret_value().encode())

    async def get_client(self, client_id):
        value = self.db.secret_get("client", client_id)
        return OAuthClientInformationFull.model_validate(value) if value else None

    async def register_client(self, client_info):
        allowed = self.settings.oauth_redirect_hosts.split(",")
        for uri in client_info.redirect_uris or []:
            url = urlsplit(str(uri))
            if (url.scheme != "https" or url.hostname not in allowed or url.port not in {None, 443}
                    or url.username or url.fragment):
                raise RegistrationError("invalid_redirect_uri", "Callback host must be owner-allowlisted HTTPS")
        # No URL fetching or automatic trust in names/software metadata.
        self.db.secret_put("client", client_info.client_id, client_info.model_dump(mode="json"), time.time() + 365 * 86400)

    async def authorize(self, client, params: AuthorizationParams):
        if params.resource not in {None, self.settings.mcp_url}:
            raise AuthorizeError("invalid_request", "Resource mismatch")
        if set(params.scopes or []) - {"read", "trade"}:
            raise AuthorizeError("invalid_scope")
        nonce = secrets.token_urlsafe(32)
        self.db.secret_put("pending", token_hash(nonce),
                           {"client_id": client.client_id, "params": params.model_dump(mode="json")}, time.time() + 300)
        return self.settings.public_base_url + "/consent?" + urlencode({"nonce": nonce})

    def consent_info(self, nonce):
        return self.db.secret_get("pending", token_hash(nonce))

    def consent(self, nonce, login_token):
        # The owner logs in with the reader/director connector secret. Never the Kalshi PEM.
        role = self.static_role(login_token)
        if not role:
            return None
        pending = self.db.secret_get("pending", token_hash(nonce), consume=True)
        if not pending:
            return None
        params = AuthorizationParams.model_validate(pending["params"])
        scopes = ["read"] + (["trade"] if role == "trade" else [])
        code = secrets.token_urlsafe(32)
        record = AuthorizationCode(code=code, scopes=scopes, expires_at=time.time() + 120,
                                   client_id=pending["client_id"], code_challenge=params.code_challenge,
                                   redirect_uri=params.redirect_uri,
                                   redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
                                   resource=self.settings.mcp_url, subject="robby")
        self.db.secret_put("code", token_hash(code), record.model_dump(mode="json", exclude={"code"}), record.expires_at)
        from mcp.server.auth.provider import construct_redirect_uri
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(self, client, authorization_code):
        value = self.db.secret_get("code", token_hash(authorization_code))
        if not value or value["client_id"] != client.client_id:
            return None
        return AuthorizationCode(code=authorization_code, **value)

    async def exchange_authorization_code(self, client, authorization_code):
        value = self.db.secret_get("code", token_hash(authorization_code.code), consume=True)
        if not value or value["client_id"] != client.client_id:
            raise TokenError("invalid_grant")
        return self.issue(client.client_id, authorization_code.scopes)

    def issue(self, client_id, scopes):
        access, refresh = secrets.token_urlsafe(48), secrets.token_urlsafe(48)
        family = secrets.token_urlsafe(32)
        access_value = AccessToken(token=access, client_id=client_id, scopes=scopes,
                                   expires_at=int(time.time()) + 3600, resource=self.settings.mcp_url, subject="robby")
        refresh_value = RefreshToken(token=refresh, client_id=client_id, scopes=scopes,
                                     expires_at=int(time.time()) + 30 * 86400, resource=self.settings.mcp_url, subject="robby")
        self.db.secret_put("access", token_hash(access),
                           {**access_value.model_dump(exclude={"token"}), "family": family}, access_value.expires_at)
        self.db.secret_put("refresh", token_hash(refresh),
                           {**refresh_value.model_dump(exclude={"token"}), "family": family,
                            "access_hash": token_hash(access)}, refresh_value.expires_at)
        self.db.secret_put("family", family, {"revoked": False}, refresh_value.expires_at)
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=3600,
                          refresh_token=refresh, scope=" ".join(scopes))

    async def load_refresh_token(self, client, refresh_token):
        value = self.db.secret_get("refresh", token_hash(refresh_token))
        if not value or value["client_id"] != client.client_id or not self.family_valid(value):
            return None
        return RefreshToken(token=refresh_token, **{k: v for k, v in value.items() if k not in {"family", "access_hash"}})

    def family_valid(self, value):
        family = self.db.secret_get("family", value["family"])
        return family is not None and not family["revoked"]

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        value = self.db.secret_get("refresh", token_hash(refresh_token.token), consume=True)
        if not value or value["client_id"] != client.client_id or not self.family_valid(value):
            raise TokenError("invalid_grant")
        if set(scopes) - set(refresh_token.scopes):
            raise TokenError("invalid_scope")
        self.db.secret_put("family", value["family"], {"revoked": True}, time.time() + 31 * 86400)
        return self.issue(client.client_id, scopes)

    async def load_access_token(self, token):
        return await self.verify_token(token)

    async def verify_token(self, token):
        role = self.static_role(token)
        if role:
            return AccessToken(token=token, client_id="sports-director" if role == "trade" else "reader",
                               scopes=["read"] + (["trade"] if role == "trade" else []), resource=self.settings.mcp_url,
                               subject="robby")
        value = self.db.secret_get("access", token_hash(token))
        if not value or not self.family_valid(value):
            return None
        return AccessToken(token=token, **{k: v for k, v in value.items() if k != "family"})

    async def revoke_token(self, token):
        kind = "access" if isinstance(token, AccessToken) else "refresh"
        value = self.db.secret_get(kind, token_hash(token.token), consume=True)
        if value:
            self.db.secret_put("family", value["family"], {"revoked": True}, time.time() + 31 * 86400)
