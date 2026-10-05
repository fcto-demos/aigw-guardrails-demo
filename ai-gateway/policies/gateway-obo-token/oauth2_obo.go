/*
 *  Copyright (c) 2026, WSO2 LLC. (http://www.wso2.org) All Rights Reserved.
 *
 *  Licensed under the Apache License, Version 2.0 (the "License");
 *  you may not use this file except in compliance with the License.
 *  You may obtain a copy of the License at
 *
 *  http://www.apache.org/licenses/LICENSE-2.0
 *
 *  Unless required by applicable law or agreed to in writing, software
 *  distributed under the License is distributed on an "AS IS" BASIS,
 *  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 *  See the License for the specific language governing permissions and
 *  limitations under the License.
 *
 */

// Package oauth2obo implements the Entra On-Behalf-Of exchange as a gateway
// policy: the validated inbound user token (the assertion) is exchanged at the
// configured token endpoint (grant_type=jwt-bearer, requested_token_use=
// on_behalf_of) for a per-user upstream token, which replaces the inbound
// credential before the request is forwarded. Fork of oauth2-generator v0.9.0
// with the per-policy-instance token source replaced by a per-assertion cache.
package oauth2obo

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"

	policy "github.com/wso2/api-platform/sdk/core/policy/v1alpha2"
)

const (
	// AuthType is the AuthContext.AuthType this policy stamps.
	AuthType = "oauth2-obo"

	// GrantTypeJWTBearer is Entra's OBO grant (its RFC 8693 stand-in).
	GrantTypeJWTBearer = "urn:ietf:params:oauth:grant-type:jwt-bearer"

	// RequestedTokenUseOnBehalfOf is the only requested_token_use this policy
	// supports; the parameter is an enum so a typo cannot silently change semantics.
	RequestedTokenUseOnBehalfOf = "on_behalf_of"

	// MetadataKeyCacheKey carries the request's cache key from the request phase
	// to the response phase, so an upstream 401 purges only that entry.
	MetadataKeyCacheKey = "oauth2obo.cacheKey"

	// MetadataKeyPrmPointer is where mcp-auth-passthrough stashes the gateway's
	// PRM pointer; used in 401 challenges when challengeResourceMetadata is unset.
	MetadataKeyPrmPointer = "mcpauth.prmPointer"

	// ClientAuthMethodBasic (client_secret_basic) sends client ID/secret via HTTP Basic auth.
	ClientAuthMethodBasic = "client_secret_basic"

	// ClientAuthMethodPost (client_secret_post) sends client ID/secret as form
	// fields - this policy's default, and what Entra's OBO documentation shows.
	ClientAuthMethodPost = "client_secret_post"

	// defaultExpiryBuffer is how far ahead of expiry a cached token is treated as
	// stale. Entra OBO tokens live ~60-75 min; 2m keeps the cache useful while
	// never sending a credential that expires mid-flight.
	defaultExpiryBuffer = 2 * time.Minute

	// defaultCacheMaxEntries bounds the per-assertion cache.
	defaultCacheMaxEntries = 10000

	// minAssertionLength rejects obviously-not-a-token header values defensively.
	minAssertionLength = 20
)

// oboParams bundles all extracted, validated policy params.
type oboParams struct {
	tokenEndpoint    string
	clientID         string
	clientSecret     string
	clientAuthMethod string

	// upstreamScopes is the space-separated scope value sent for token B.
	upstreamScopes string

	// assertionHeader is the downstream-snapshot header the inbound token (the
	// assertion) is read from.
	assertionHeader string

	// requireAuthContext fails closed with 401 when no upstream auth policy has
	// validated the inbound token, so this policy can never exchange an
	// unvalidated assertion when attached alone.
	requireAuthContext bool

	requestedTokenUse string

	cacheMaxEntries int

	// challengeResourceMetadata is the PRM URL for 401 challenges; empty falls
	// back to Metadata[MetadataKeyPrmPointer].
	challengeResourceMetadata string

	// userIdClaim is the claim of token B copied into AuthContext.Subject.
	userIdClaim string

	tokenRequestParams  map[string]string
	tokenRequestHeaders map[string]string
	tokenRequestTimeout time.Duration
	defaultTokenTTL     time.Duration
	expiryBuffer        time.Duration

	tokenPurgeStatusCodes map[int]struct{}

	headerName  string
	valuePrefix string

	proxyURL              string
	tlsCaCert             string
	tlsInsecureSkipVerify bool

	tokenRequestMaxRetries int
}

// Policy exchanges the validated inbound bearer token for a per-user upstream
// token via the On-Behalf-Of grant and injects it into headerName.
type Policy struct {
	params oboParams

	httpClient *http.Client
	cache      *tokenCache
	flight     *flightGroup
}

// GetPolicy is the v1alpha2 factory entry point (loaded by v1alpha2 kernels).
func GetPolicy(metadata policy.PolicyMetadata, params map[string]interface{}) (policy.Policy, error) {
	slog.Debug("OAuth2OBO: constructing policy from params")

	p, err := validateAndExtractParams(params)
	if err != nil {
		return nil, fmt.Errorf("invalid params: %w", err)
	}

	transport, err := getOrCreateTokenEndpointTransport(p)
	if err != nil {
		return nil, fmt.Errorf("invalid token endpoint transport config: %w", err)
	}

	pol := &Policy{
		params:     p,
		httpClient: &http.Client{Timeout: p.tokenRequestTimeout, Transport: transport},
		cache:      newTokenCache(),
		flight:     newFlightGroup(),
	}

	slog.Debug("OAuth2OBO: policy initialized",
		"tokenEndpoint", sanitizeEndpointForLogging(p.tokenEndpoint), "clientId", p.clientID,
		"clientAuthMethod", p.clientAuthMethod, "headerName", p.headerName,
		"assertionHeader", p.assertionHeader, "requireAuthContext", p.requireAuthContext)

	return pol, nil
}

// validateAndExtractParams validates and extracts all policy params.
func validateAndExtractParams(params map[string]interface{}) (oboParams, error) {
	var p oboParams
	var err error

	p.tokenEndpoint, err = getRequiredStringParam(params, "tokenEndpoint")
	if err != nil {
		return oboParams{}, err
	}
	p.clientID, err = getRequiredStringParam(params, "clientId")
	if err != nil {
		return oboParams{}, err
	}
	p.clientSecret, err = getRequiredStringParam(params, "clientSecret")
	if err != nil {
		return oboParams{}, err
	}
	p.upstreamScopes, err = getRequiredStringParam(params, "upstreamScopes")
	if err != nil {
		return oboParams{}, err
	}

	p.clientAuthMethod = getStringParam(params, "clientAuthMethod")
	if p.clientAuthMethod == "" {
		p.clientAuthMethod = ClientAuthMethodPost
	}
	if p.clientAuthMethod != ClientAuthMethodBasic && p.clientAuthMethod != ClientAuthMethodPost {
		return oboParams{}, fmt.Errorf("'clientAuthMethod' must be one of %q, %q", ClientAuthMethodBasic, ClientAuthMethodPost)
	}

	p.requestedTokenUse = getStringParamOrDefault(params, "requestedTokenUse", RequestedTokenUseOnBehalfOf)
	if p.requestedTokenUse != RequestedTokenUseOnBehalfOf {
		return oboParams{}, fmt.Errorf("'requestedTokenUse' must be %q", RequestedTokenUseOnBehalfOf)
	}

	p.assertionHeader = getStringParamOrDefault(params, "assertionHeader", "Authorization")
	if p.assertionHeader == "" {
		p.assertionHeader = "Authorization"
	}
	p.requireAuthContext = getBoolParam(params, "requireAuthContext", true)
	p.userIdClaim = getStringParamOrDefault(params, "userIdClaim", "oid")
	if p.userIdClaim == "" {
		p.userIdClaim = "oid"
	}
	p.challengeResourceMetadata = getStringParam(params, "challengeResourceMetadata")

	p.cacheMaxEntries = getIntParam(params, "cacheMaxEntries", defaultCacheMaxEntries)
	if p.cacheMaxEntries < 1 {
		p.cacheMaxEntries = defaultCacheMaxEntries
	}

	p.headerName = getStringParamOrDefault(params, "headerName", defaultHeaderName)
	if p.headerName == "" {
		p.headerName = defaultHeaderName
	}
	p.valuePrefix = getStringParamOrDefault(params, "valuePrefix", defaultValuePrefix)

	p.tokenRequestParams = getStringMapParam(params, "tokenRequestParams")
	p.tokenRequestHeaders = getStringMapParam(params, "tokenRequestHeaders")
	p.tokenRequestTimeout = getPositiveDurationParam(params, "tokenRequestTimeout", defaultTokenRequestTimeout)
	p.defaultTokenTTL = getPositiveDurationParam(params, "defaultTokenTTL", defaultTokenTTLFallback)
	p.expiryBuffer = getDurationParam(params, "expiryBuffer", defaultExpiryBuffer)
	if p.expiryBuffer < 0 {
		p.expiryBuffer = defaultExpiryBuffer
	}
	p.tokenPurgeStatusCodes = getPurgeStatusCodesParam(params, "tokenPurgeStatusCodes", defaultPurgeStatusCodes)

	p.proxyURL = getStringParam(params, "proxyURL")
	p.tlsCaCert = getStringParam(params, "tlsCaCert")
	p.tlsInsecureSkipVerify = getBoolParam(params, "tlsInsecureSkipVerify", false)
	if p.tlsInsecureSkipVerify {
		slog.Warn("OAuth2OBO: tlsInsecureSkipVerify is enabled - TLS certificate verification for the token endpoint is disabled; this must never be used against a real identity provider")
	}

	p.tokenRequestMaxRetries = getIntParam(params, "tokenRequestMaxRetries", defaultTokenRequestMaxRetries)

	return p, nil
}

// Mode declares processing per phase. Bodies are never buffered - /mcp streams
// SSE. Response headers are processed only when purging is enabled.
func (p *Policy) Mode() policy.ProcessingMode {
	responseHeaderMode := policy.HeaderModeSkip
	if len(p.params.tokenPurgeStatusCodes) > 0 {
		responseHeaderMode = policy.HeaderModeProcess
	}
	return policy.ProcessingMode{
		RequestHeaderMode:  policy.HeaderModeProcess,
		RequestBodyMode:    policy.BodyModeSkip,
		ResponseHeaderMode: responseHeaderMode,
		ResponseBodyMode:   policy.BodyModeSkip,
	}
}

// OnRequestHeaders performs the OBO exchange (or serves it from the
// per-assertion cache) and swaps the inbound credential for token B.
func (p *Policy) OnRequestHeaders(ctx context.Context, req *policy.RequestHeaderContext, _ map[string]interface{}) policy.RequestHeaderAction {
	// 0. Only act on requests that reached us through the inbound auth policy.
	if p.params.requireAuthContext && (req.SharedContext == nil || req.AuthContext == nil || !req.AuthContext.Authenticated) {
		slog.Warn("OAuth2OBO: rejecting request - inbound token not validated by an upstream auth policy")
		return p.unauthorized(req, "inbound token not validated", nil)
	}

	// 1. Assertion = the ORIGINAL client token from the pre-mutation snapshot,
	// independent of whether the inbound policy stripped or forwarded it.
	var snapshot *policy.Headers
	if req.Downstream != nil && req.Downstream.Request != nil {
		snapshot = req.Downstream.Request.Headers
	}
	if snapshot == nil {
		slog.Warn("OAuth2OBO: downstream request snapshot unavailable")
		return p.unauthorized(req, "missing inbound bearer token", nil)
	}
	assertion := bearerFrom(snapshot.Get(p.params.assertionHeader))
	if assertion == "" {
		return p.unauthorized(req, "missing inbound bearer token", nil)
	}

	key := sha256hex(assertion)
	p.stashCacheKey(req.SharedContext, key)

	// 2. Cache lookup by sha256(assertion).
	if tok, ok := p.cache.get(key, p.params.expiryBuffer); ok {
		return p.inject(req, tok, true)
	}

	// 3. OBO call, single-flighted per key so N concurrent requests carrying the
	// same assertion produce one token-endpoint call.
	tok, err := p.flight.Do(key, func() (*oboToken, error) {
		return p.exchange(ctx, assertion, key)
	})
	if err != nil {
		var te *TokenError
		if errors.As(err, &te) && isReauthError(te) {
			slog.Warn("OAuth2OBO: exchange rejected - client must re-authenticate",
				"error", te.ErrorCode, "aadsts", aadstsCode(te.ErrorDescription),
				"correlationId", te.CorrelationID, "cacheKeyId", key[:8])
			return p.unauthorized(req, sanitizeAADDescription(te.ErrorDescription), te)
		}
		slog.Error("OAuth2OBO: credential acquisition failed",
			"error", redactURLCredentials(err.Error()),
			"tokenEndpoint", sanitizeEndpointForLogging(p.params.tokenEndpoint),
			"clientId", p.params.clientID, "cacheKeyId", key[:8])
		return p.badGateway(req)
	}
	return p.inject(req, tok, false)
}

// exchange performs the OBO token request (with retry) and caches the result.
// TTL is bounded by min(expires_in of B, exp claim of the assertion) - the
// buffer is applied at read time by tokenCache.get.
func (p *Policy) exchange(ctx context.Context, assertion, key string) (*oboToken, error) {
	form := url.Values{}
	for k, v := range p.params.tokenRequestParams {
		form.Set(k, v)
	}
	// Core fields are set last so tokenRequestParams extras can never override them.
	form.Set("grant_type", GrantTypeJWTBearer)
	form.Set("assertion", assertion)
	form.Set("requested_token_use", p.params.requestedTokenUse)
	form.Set("scope", p.params.upstreamScopes)

	style := authStyleFor(p.params.clientAuthMethod)
	tok, err := fetchWithRetry(ctx, p.params.tokenRequestMaxRetries, func(ctx context.Context) (*Token, error) {
		return doTokenRequest(ctx, p.httpClient, p.params.tokenEndpoint, style,
			p.params.clientID, p.params.clientSecret, cloneURLValues(form), p.params.tokenRequestHeaders)
	})
	if err != nil {
		return nil, err
	}

	if tok.Expiry.IsZero() {
		tok.Expiry = time.Now().Add(p.params.defaultTokenTTL)
	}
	if expA, ok := jwtExp(assertion); ok && expA.Before(tok.Expiry) {
		tok.Expiry = expA
	}

	obo := &oboToken{Token: *tok, claims: jwtClaims(tok.AccessToken)}
	p.cache.put(key, obo, p.params.cacheMaxEntries)
	slog.Debug("OAuth2OBO: token exchanged and cached", "cacheKeyId", key[:8],
		"expiry", obo.Expiry.Format(time.RFC3339))
	return obo, nil
}

// OnResponseHeaders purges only the requesting assertion's cache entry when the
// upstream rejects the exchanged token (default: 401), so the next request for
// that user re-exchanges while other users' entries stay. Challenge rewriting
// is deliberately NOT done here - mcp-auth-passthrough owns it.
func (p *Policy) OnResponseHeaders(ctx context.Context, respCtx *policy.ResponseHeaderContext, _ map[string]interface{}) policy.ResponseHeaderAction {
	if _, purge := p.params.tokenPurgeStatusCodes[respCtx.ResponseStatus]; purge {
		if key := p.stashedCacheKey(respCtx.SharedContext); key != "" {
			slog.Warn("OAuth2OBO: upstream rejected the exchanged token, purging its cache entry",
				"status", respCtx.ResponseStatus, "cacheKeyId", key[:8])
			p.cache.delete(key)
		}
	}
	return policy.DownstreamResponseHeaderModifications{}
}

// stashCacheKey records the request's cache key in shared metadata for the
// response phase.
func (p *Policy) stashCacheKey(shared *policy.SharedContext, key string) {
	if shared == nil {
		return
	}
	if shared.Metadata == nil {
		shared.Metadata = map[string]any{}
	}
	shared.Metadata[MetadataKeyCacheKey] = key
}

func (p *Policy) stashedCacheKey(shared *policy.SharedContext) string {
	if shared == nil || shared.Metadata == nil {
		return ""
	}
	key, _ := shared.Metadata[MetadataKeyCacheKey].(string)
	return key
}

// inject swaps the credential: token B goes into headerName, and the inbound
// assertion header is removed when it is a different header (when both are
// Authorization, HeadersToSet overwrites - either way the client token never
// goes upstream).
func (p *Policy) inject(req *policy.RequestHeaderContext, tok *oboToken, cached bool) policy.RequestHeaderAction {
	cacheState := "miss"
	if cached {
		cacheState = "hit"
	}
	p.authSuccess(req, tok, cacheState)

	var remove []string
	if !strings.EqualFold(p.params.assertionHeader, p.params.headerName) {
		remove = []string{p.params.assertionHeader}
	}
	return policy.UpstreamRequestHeaderModifications{
		HeadersToSet: map[string]string{
			p.params.headerName: buildHeaderValue(p.params.valuePrefix, tok.AccessToken),
		},
		HeadersToRemove:   remove,
		AnalyticsMetadata: map[string]any{"obo.cache": cacheState},
	}
}

// authSuccess records the exchange in the shared AuthContext using token B's
// claims, chaining the inbound policy's context via Previous.
func (p *Policy) authSuccess(req *policy.RequestHeaderContext, tok *oboToken, cacheState string) {
	if req.SharedContext == nil {
		return
	}
	prev := req.AuthContext

	subject := claimString(tok.claims, p.params.userIdClaim)
	if subject == "" && prev != nil {
		subject = prev.Subject
	}
	properties := map[string]string{
		"mode":          RequestedTokenUseOnBehalfOf,
		"tokenEndpoint": sanitizeEndpointForLogging(p.params.tokenEndpoint),
		"cache":         cacheState,
	}
	if pu := claimString(tok.claims, "preferred_username"); pu != "" {
		properties["preferred_username"] = pu
	}
	req.SharedContext.AuthContext = &policy.AuthContext{
		Authenticated: true,
		AuthType:      AuthType,
		Subject:       subject,
		Issuer:        claimString(tok.claims, "iss"),
		Audience:      claimStrings(tok.claims, "aud"),
		Scopes:        scopesFromClaims(tok.claims),
		CredentialID:  p.params.clientID,
		Properties:    properties,
		Previous:      prev,
	}
}

// unauthorized builds the 401 that tells the client to (re-)authenticate: RFC
// 6750 invalid_token plus, when known, the resource_metadata pointer to the
// gateway's PRM so MCP clients restart discovery in the right place.
func (p *Policy) unauthorized(req *policy.RequestHeaderContext, desc string, cause *TokenError) policy.RequestHeaderAction {
	if req.SharedContext != nil {
		req.SharedContext.AuthContext = &policy.AuthContext{
			Authenticated: false,
			AuthType:      AuthType,
			Previous:      req.AuthContext,
		}
	}

	desc = strings.ReplaceAll(sanitizeAADDescription(desc), `"`, "'")
	challenge := `Bearer error="invalid_token"`
	if desc != "" {
		challenge += `, error_description="` + desc + `"`
	}
	if ptr := p.challengePointer(req.SharedContext); ptr != "" {
		challenge += `, resource_metadata="` + ptr + `"`
	}

	body, _ := json.Marshal(map[string]string{
		"error":             "invalid_token",
		"error_description": desc,
	})
	return policy.ImmediateResponse{
		StatusCode: http.StatusUnauthorized,
		Headers: map[string]string{
			"Content-Type":     "application/json",
			"WWW-Authenticate": challenge,
		},
		Body: body,
	}
}

// challengePointer resolves the PRM URL for the 401 challenge: the explicit
// parameter wins, then the pointer mcp-auth-passthrough stashed in metadata.
func (p *Policy) challengePointer(shared *policy.SharedContext) string {
	if p.params.challengeResourceMetadata != "" {
		return p.params.challengeResourceMetadata
	}
	if shared != nil && shared.Metadata != nil {
		if v, ok := shared.Metadata[MetadataKeyPrmPointer].(string); ok {
			return v
		}
	}
	return ""
}

// badGateway is the gateway-side-fault response (D-7): the client's token was
// fine, the gateway could not mint the upstream credential - a 502 keeps
// clients from looping on sign-in.
func (p *Policy) badGateway(req *policy.RequestHeaderContext) policy.RequestHeaderAction {
	if req.SharedContext != nil {
		req.SharedContext.AuthContext = &policy.AuthContext{
			Authenticated: false,
			AuthType:      AuthType,
			CredentialID:  p.params.clientID,
			Properties:    map[string]string{"mode": RequestedTokenUseOnBehalfOf, "tokenEndpoint": sanitizeEndpointForLogging(p.params.tokenEndpoint)},
			Previous:      req.AuthContext,
		}
	}
	body, _ := json.Marshal(map[string]string{
		"error":   "Bad Gateway",
		"message": "failed to authenticate request to upstream service",
	})
	return policy.ImmediateResponse{
		StatusCode: http.StatusBadGateway,
		Headers:    map[string]string{"Content-Type": "application/json"},
		Body:       body,
	}
}

// reauthAADSTSCodes are Entra error codes that mean "the user's token or grant
// state is the problem" - a fresh sign-in can fix them, so they map to 401.
var reauthAADSTSCodes = []string{
	"AADSTS50013", "AADSTS70008", "AADSTS700084", "AADSTS500133",
	"AADSTS50076", "AADSTS50079", "AADSTS53000", "AADSTS65001",
}

// isReauthError classifies a token-endpoint rejection: true means the client
// re-authenticating can fix it (401), false means it is the gateway's own
// configuration or the IdP (502).
func isReauthError(te *TokenError) bool {
	switch te.ErrorCode {
	case "invalid_grant", "interaction_required", "unauthorized_client":
		return true
	}
	for _, code := range reauthAADSTSCodes {
		if strings.Contains(te.ErrorDescription, code) {
			return true
		}
	}
	return false
}

var aadstsRe = regexp.MustCompile(`AADSTS\d+`)

// aadstsCode extracts the AADSTS error code from an Entra error_description,
// for logging without the full description.
func aadstsCode(desc string) string {
	return aadstsRe.FindString(desc)
}

// sanitizeAADDescription trims an Entra error_description to the part before
// any trace/correlation identifiers, so responses and challenges carry the
// AADSTS code and message but no request-tracing IDs.
func sanitizeAADDescription(desc string) string {
	for _, marker := range []string{"Trace ID:", "Correlation ID:", "Timestamp:"} {
		if i := strings.Index(desc, marker); i >= 0 {
			desc = desc[:i]
		}
	}
	return strings.TrimSpace(desc)
}

// bearerFrom extracts the bare token from the header values: first non-empty
// value, "Bearer " scheme stripped case-insensitively, defensively rejecting
// values with embedded whitespace or implausibly short ones.
func bearerFrom(vals []string) string {
	var raw string
	for _, v := range vals {
		if strings.TrimSpace(v) != "" {
			raw = strings.TrimSpace(v)
			break
		}
	}
	if raw == "" {
		return ""
	}
	if len(raw) > len("bearer ") && strings.EqualFold(raw[:len("bearer ")], "bearer ") {
		raw = strings.TrimSpace(raw[len("bearer "):])
	}
	if len(raw) < minAssertionLength || strings.ContainsAny(raw, " \t") {
		return ""
	}
	return raw
}

// jwtPayload base64url-decodes a JWT's payload segment without any signature
// check - the inbound policy already validated the assertion, and token B comes
// straight from the token endpoint over TLS.
func jwtPayload(token string) (map[string]any, bool) {
	parts := strings.Split(token, ".")
	if len(parts) < 2 {
		return nil, false
	}
	data, err := base64.RawURLEncoding.DecodeString(strings.TrimRight(parts[1], "="))
	if err != nil {
		return nil, false
	}
	var claims map[string]any
	if err := json.Unmarshal(data, &claims); err != nil {
		return nil, false
	}
	return claims, true
}

// jwtExp reads the exp claim of a JWT; ok=false on any parse failure (the
// caller falls back to expires_in).
func jwtExp(token string) (time.Time, bool) {
	claims, ok := jwtPayload(token)
	if !ok {
		return time.Time{}, false
	}
	exp, ok := claims["exp"].(float64)
	if !ok {
		return time.Time{}, false
	}
	return time.Unix(int64(exp), 0), true
}

// jwtClaims returns the decoded payload, or an empty map when the token is not
// parseable as a JWT (AuthContext then simply carries no claim-derived fields).
func jwtClaims(token string) map[string]any {
	claims, ok := jwtPayload(token)
	if !ok {
		return map[string]any{}
	}
	return claims
}

func claimString(claims map[string]any, name string) string {
	if v, ok := claims[name].(string); ok {
		return v
	}
	return ""
}

// claimStrings reads a claim that may be a string or an array of strings (aud).
func claimStrings(claims map[string]any, name string) []string {
	switch v := claims[name].(type) {
	case string:
		return []string{v}
	case []any:
		out := make([]string, 0, len(v))
		for _, item := range v {
			if s, ok := item.(string); ok {
				out = append(out, s)
			}
		}
		return out
	}
	return nil
}

// scopesFromClaims reads scopes from scp (Entra: space-separated string, or an
// array) or scope, into the AuthContext.Scopes shape.
func scopesFromClaims(claims map[string]any) map[string]bool {
	var raw []string
	for _, name := range []string{"scp", "scope"} {
		switch v := claims[name].(type) {
		case string:
			raw = strings.Fields(v)
		case []any:
			for _, item := range v {
				if s, ok := item.(string); ok {
					raw = append(raw, s)
				}
			}
		}
		if len(raw) > 0 {
			break
		}
	}
	if len(raw) == 0 {
		return nil
	}
	scopes := make(map[string]bool, len(raw))
	for _, s := range raw {
		scopes[s] = true
	}
	return scopes
}

// cloneURLValues copies form values so a retry never observes a mutated map.
func cloneURLValues(v url.Values) url.Values {
	out := make(url.Values, len(v))
	for k, vals := range v {
		out[k] = append([]string(nil), vals...)
	}
	return out
}
