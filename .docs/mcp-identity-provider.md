# Identity provider for the MCP Authentication and Authorization cards

These two cards need an OpenID Connect provider that signs users in and issues
JWT access tokens with OAuth scopes. Any provider works; the steps below are
for Asgardeo and WSO2 Identity Server, which share the same console.

## Set it up in Asgardeo or WSO2 Identity Server

1. **MCP server** — *MCP Servers* → *New MCP Server*: identifier
   `https://acme-mobile/mcp`, scopes `acme:plan:read` and `acme:plan:write`.
2. **Application** — *Applications* → *New Application* → *MCP Client
   Application*:
   - redirect URL `http://localhost:8600/oauth/callback`;
   - *Protocol*: access token type JWT, access token attributes `username`
     and `email` (the name shown as "Signed in as ...");
   - *API Authorization*: authorize the MCP server with both scopes;
   - copy the **Client ID**.
3. **Roles** — in the application's *Roles* tab: `acme-support` with
   `acme:plan:read`, `acme-manager` with both scopes.
4. **Users** — two users, one per role.
5. **secrets/idp.env** — `cp secrets/idp.env.example secrets/idp.env`, then:

   ```
   IDP_ISSUER=<issuer>          # Asgardeo: https://api.asgardeo.io/t/<org>/oauth2/token
   IDP_JWKS_URI=<jwks_uri>      # Asgardeo: https://api.asgardeo.io/t/<org>/oauth2/jwks
   IDP_CLIENT_ID=<client ID>
   ```

   Both URLs are in the provider's `<issuer>/.well-known/openid-configuration`.
   For a provider on this machine, write `host.docker.internal` instead of
   `localhost` in `IDP_JWKS_URI` (the gateway fetches it from its container).
6. **Apply**:

   ```bash
   ./scripts/run-local.sh
   ./scripts/python/provision_showcases.py mcp-authentication mcp-authorization
   ```

With a publicly trusted certificate (Asgardeo), set `skipTlsVerify = false`
on the `mcp-idp` key manager in `ai-gateway/configs/config.toml`.

## Other providers

Same steps. The scopes must reach the token per role (only one user gets
`acme:plan:write`), in a `scope` claim — Microsoft Entra ID uses `scp`: add
`scopeClaim = "scp"` to the `mcp-idp` key manager.

## How Sign in works

The page does what any MCP client (Claude Code, for one) does with a 401:

1. Without a token, `mcp-auth` answers **401**, pointing to the proxy's
   protected resource metadata: the identity provider and the scopes.
2. Nothing logs in by itself: the client asks the user (Claude Code lists the
   server under `/mcp`; the page shows *Sign in*), then opens the provider's
   login page — authorization code + PKCE, as `IDP_CLIENT_ID`.
3. The token goes on that proxy's MCP calls only, never to the model.
   `mcp-authz` then requires `acme:plan:write` for `change_plan` (else **403**).

An external client follows the 401's URL as-is: the gateway advertises
`DEMO_HOSTNAME` (`demo.env`), which it must be able to reach.
