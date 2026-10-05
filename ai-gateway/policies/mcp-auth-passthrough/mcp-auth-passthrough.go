/*
 * Copyright (c) 2025, WSO2 LLC. (https://www.wso2.com).
 *
 * WSO2 LLC. licenses this file to you under the Apache License,
 * Version 2.0 (the "License"); you may not use this file except
 * in compliance with the License.
 * You may obtain a copy of the License at
 *
 * http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing,
 * software distributed under the License is distributed on an
 * "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
 * KIND, either express or implied.  See the License for the
 * specific language governing permissions and limitations
 * under the License.
 */

package mcpauthn

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"maps"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	policy "github.com/wso2/api-platform/sdk/core/policy/v1alpha2"
	jwtauth "github.com/wso2/gateway-controllers/policies/jwt-auth"
)

const (
	WWWAuthenticateHeader  = "WWW-Authenticate"
	AuthMethodBearer       = "Bearer resource_metadata="
	WellKnownPath          = ".well-known/oauth-protected-resource"
	WellKnownEndpointPath  = "/" + WellKnownPath
	McpPathSegment         = "mcp"
	McpSessionHeader       = "mcp-session-id"
	AuthType               = "mcp/oauth"
	MetadataKeyAuthSuccess = "auth.success"
	MetadataKeyAuthMethod  = "auth.method"
	MetadataKeyPrmPointer  = "mcpauth.prmPointer"

	// Gateway-challenge modes: how the gateway's OWN authentication failure
	// (a missing or invalid gateway token) is signalled in WWW-Authenticate.
	// This is distinct from the upstream MCP server's 401, which the response
	// phase rewrites (see OnResponseHeaders) and which is what MCP clients
	// should follow to obtain the MCP server's token.
	//
	//   gateway (default): `Bearer realm="<gatewayChallengeRealm>", ...` with NO
	//     resource_metadata pointer. The gateway's PRM describes the upstream
	//     MCP server's authorization server, so pointing a client at it for a
	//     gateway-credential problem sends it to obtain a token that cannot fix
	//     the failure. error="invalid_token" is set only when a gateway token was
	//     actually present and rejected (RFC 6750 §3.1); when the header is
	//     absent no error code is emitted, only an error_description naming the
	//     header the gateway expects.
	//   legacy: the shipped mcp-auth behaviour — pointer to the gateway PRM plus
	//     error="invalid_token" regardless of whether a token was present.
	//   none: omit WWW-Authenticate entirely (same as suppressGatewayChallenge=true).
	GatewayChallengeModeGateway  = "gateway"
	GatewayChallengeModeLegacy   = "legacy"
	GatewayChallengeModeNone     = "none"
	DefaultGatewayChallengeRealm = "wso2-ai-gateway"

	// JSON-RPC 2.0 constants. MCP is JSON-RPC 2.0, so protocol-level errors
	// (as opposed to HTTP/OAuth auth failures) must be surfaced as JSON-RPC
	// error objects. See https://www.jsonrpc.org/specification#error_object.
	JSONRPCVersion        = "2.0"
	JSONRPCParseError     = -32700 // invalid JSON was received
	JSONRPCInvalidRequest = -32600 // valid JSON that is not a valid request object
)

type McpAuthPolicy struct {
	AuthConfig               McpAuthConfig `json:"authConfig"`
	Issuers                  []string      `json:"issuers"`
	RequiredScopes           []string      `json:"requiredScopes"`
	PrmScopesSupported       []string      `json:"prmScopesSupported"`
	OnFailureStatusCode      int           `json:"onFailureStatusCode"`
	ErrorMessageFormat       string        `json:"errorMessageFormat"`
	GatewayHost              string        `json:"gatewayHost"`
	PrmUpstreamURL           string        `json:"prmUpstreamUrl"`
	PrmCacheSeconds          int           `json:"prmCacheSeconds"`
	PrmFetchTimeoutSeconds   int           `json:"prmFetchTimeoutSeconds"`
	RewriteChallenge         bool          `json:"rewriteChallenge"`
	SuppressGatewayChallenge bool          `json:"suppressGatewayChallenge"` // deprecated: use gatewayChallengeMode=none
	GatewayChallengeMode     string        `json:"gatewayChallengeMode"`
	GatewayChallengeRealm    string        `json:"gatewayChallengeRealm"`

	// httpClient fetches the upstream PRM; built in GetPolicy, injectable in tests.
	httpClient *http.Client
	// prmCache holds the last successfully fetched upstream PRM document. The
	// policy instance is per attachment, so this is one cache per proxy.
	prmCache prmCache
}

// prmCache is the cached upstream PRM document plus its fetch time.
type prmCache struct {
	mu        sync.RWMutex
	body      []byte
	fetchedAt time.Time
}

type ProtectedResourceMetadata struct {
	Resource             string   `json:"resource"`
	AuthorizationServers []string `json:"authorization_servers"`
	ScopesSupported      []string `json:"scopes_supported,omitempty"`
}

// SecurityConfig represents the configuration for tools, resources, prompts, or methods
type SecurityConfig struct {
	Enabled    bool     `json:"enabled"`
	Exceptions []string `json:"exceptions"`
}

// McpAuthConfig holds the parsed MCP auth configuration
type McpAuthConfig struct {
	Tools     SecurityConfig
	Resources SecurityConfig
	Prompts   SecurityConfig
	Methods   SecurityConfig
}

// MCPRequest represents the JSON-RPC MCP request structure
type MCPRequest struct {
	Method string           `json:"method"`
	Params MCPRequestParams `json:"params"`
}

// MCPRequestParams represents the params section of an MCP request
// Different MCP methods use different param structures:
// - tools/call: uses "name" (tool name) and "arguments"
// - resources/read: uses "uri" (resource URI)
// - prompts/get: uses "name" (prompt name)
type MCPRequestParams struct {
	Name string `json:"name"` // For tools/call, prompts/get
	URI  string `json:"uri"`  // For resources/read
}

// GetPolicy is the v1alpha2 factory entry point (loaded by v1alpha2 kernels).
func GetPolicy(
	metadata policy.PolicyMetadata,
	params map[string]interface{},
) (policy.Policy, error) {
	slog.Debug("MCP Auth Policy: GetPolicy called")
	ins := &McpAuthPolicy{
		AuthConfig: GetMcpAuthConfig(params),
	}
	ins.Issuers = getStringArrayParam(params, "issuers", []string{})
	ins.RequiredScopes = getStringArrayParam(params, "requiredScopes", []string{})
	ins.PrmScopesSupported = getStringArrayParam(params, "prmScopesSupported", []string{})
	ins.OnFailureStatusCode = getIntParam(params, "onFailureStatusCode", 401)
	ins.ErrorMessageFormat = getStringParam(params, "errorMessageFormat", "json")
	ins.GatewayHost = getStringParam(params, "gatewayHost", "")
	ins.PrmUpstreamURL = getStringParam(params, "prmUpstreamUrl", "")
	ins.PrmCacheSeconds = getIntParam(params, "prmCacheSeconds", 300)
	ins.PrmFetchTimeoutSeconds = getIntParam(params, "prmFetchTimeoutSeconds", 5)
	ins.RewriteChallenge = getBoolParam(params, "rewriteChallenge", true)
	ins.SuppressGatewayChallenge = getBoolParam(params, "suppressGatewayChallenge", false)
	ins.GatewayChallengeMode = getStringParam(params, "gatewayChallengeMode", GatewayChallengeModeGateway)
	ins.GatewayChallengeRealm = getStringParam(params, "gatewayChallengeRealm", DefaultGatewayChallengeRealm)
	// The deprecated boolean keeps working: true forces mode "none".
	if ins.SuppressGatewayChallenge {
		ins.GatewayChallengeMode = GatewayChallengeModeNone
	}
	switch ins.GatewayChallengeMode {
	case GatewayChallengeModeGateway, GatewayChallengeModeLegacy, GatewayChallengeModeNone:
	default:
		return nil, fmt.Errorf("invalid policy configuration: gatewayChallengeMode must be one of [%s, %s, %s]",
			GatewayChallengeModeGateway, GatewayChallengeModeLegacy, GatewayChallengeModeNone)
	}
	ins.httpClient = &http.Client{Timeout: time.Duration(ins.PrmFetchTimeoutSeconds) * time.Second}

	return ins, nil
}

// parseAuthority extracts host and port from an authority string (e.g., "example.com:8080")
func parseAuthority(authority string) (host string, port int) {
	if authority == "" {
		return "", -1
	}
	hostPort := strings.SplitN(authority, ":", 2)
	host = hostPort[0]
	if len(hostPort) > 1 {
		port, _ = strconv.Atoi(hostPort[1])
	} else {
		port = -1
	}
	return host, port
}

// isStandardPort returns true if the port is the standard port for the given scheme
func isStandardPort(scheme string, port int) bool {
	return (scheme == "http" && port == 80) || (scheme == "https" && port == 443)
}

func getStringParam(params map[string]interface{}, key, defaultValue string) string {
	if v, ok := params[key]; ok {
		if s, ok := v.(string); ok {
			return s
		}
	}
	return defaultValue
}

func getBoolParam(params map[string]interface{}, key string, defaultValue bool) bool {
	if v, ok := params[key]; ok {
		if b, ok := v.(bool); ok {
			return b
		}
	}
	return defaultValue
}

func getIntParam(params map[string]interface{}, key string, defaultValue int) int {
	if v, ok := params[key]; ok {
		if i, ok := v.(int); ok {
			return i
		}
		if f, ok := v.(float64); ok {
			return int(f)
		}
	}
	return defaultValue
}

func getStringArrayParam(params map[string]interface{}, key string, defaultValue []string) []string {
	if v, ok := params[key]; ok {
		if arr, ok := v.([]interface{}); ok {
			var result []string
			for _, item := range arr {
				if s, ok := item.(string); ok {
					result = append(result, s)
				}
			}
			if len(result) > 0 {
				return result
			}
		}
	}
	return defaultValue
}

// getSecurityConfigParam parses a security configuration object (tools, resources, prompts, methods)
// with enabled (default: true) and exceptions (default: empty array) fields.
func getSecurityConfigParam(params map[string]any, key string) SecurityConfig {
	config := SecurityConfig{
		Enabled:    true, // default value per policy definition
		Exceptions: []string{},
	}

	if v, ok := params[key]; ok {
		if configMap, ok := v.(map[string]any); ok {
			// Parse enabled field
			if enabled, ok := configMap["enabled"]; ok {
				if b, ok := enabled.(bool); ok {
					config.Enabled = b
					slog.Debug("MCP Auth Policy", "key", key, "enabled", b)
				}
			}
			// Parse exceptions field
			if exceptions, ok := configMap["exceptions"]; ok {
				if arr, ok := exceptions.([]any); ok {
					for _, item := range arr {
						if s, ok := item.(string); ok {
							config.Exceptions = append(config.Exceptions, s)
						}
					}
					slog.Debug("MCP Auth Policy", "key", key, "exceptions", len(config.Exceptions))
				}
			}
		}
	} else {
		slog.Debug("MCP Auth Policy: No configuration found for key", "key", key)
	}

	return config
}

// GetMcpAuthConfig parses all MCP auth configuration parameters into a structured format.
func GetMcpAuthConfig(params map[string]any) McpAuthConfig {
	return McpAuthConfig{
		Tools:     getSecurityConfigParam(params, "tools"),
		Resources: getSecurityConfigParam(params, "resources"),
		Prompts:   getSecurityConfigParam(params, "prompts"),
		Methods:   getSecurityConfigParam(params, "methods"),
	}
}

// isAuthRequired determines if authentication is required for the given MCP request.
// It returns true if auth is required, false if the request is exempt based on configuration.
func (p *McpAuthPolicy) isAuthRequired(mcpReq MCPRequest) bool {
	var config SecurityConfig
	var name string

	switch mcpReq.Method {
	case "tools/call":
		config = p.AuthConfig.Tools
		name = mcpReq.Params.Name
	case "resources/read":
		config = p.AuthConfig.Resources
		name = mcpReq.Params.URI
	case "prompts/get":
		config = p.AuthConfig.Prompts
		name = mcpReq.Params.Name
	default:
		// For any other methods (e.g., "initialize", "ping", "tools/list", etc.)
		// Check if the method is in the methods exceptions list
		config = p.AuthConfig.Methods
		name = mcpReq.Method
	}

	if config.Enabled {
		if len(config.Exceptions) == 0 {
			return true
		} else {
			for _, exception := range config.Exceptions {
				if exception == name {
					slog.Debug("MCP Auth Policy: Auth not required - item in exceptions list", "method", mcpReq.Method, "name", name)
					return false
				}
			}
			return true
		}
	} else {
		if len(config.Exceptions) == 0 {
			return false
		} else {
			for _, exception := range config.Exceptions {
				if exception == name {
					slog.Debug("MCP Auth Policy: Auth required - item in exceptions list", "method", mcpReq.Method, "name", name)
					return true
				}
			}
			return false
		}
	}
}

// Helper functions for type assertions
func getString(v interface{}) string {
	if s, ok := v.(string); ok {
		return s
	}
	return ""
}

func parseKeyManagers(keyManagersRaw any) ([]string, map[string]string, error) {
	keyManagersList, ok := keyManagersRaw.([]any)
	if !ok {
		return nil, nil, fmt.Errorf("invalid policy configuration: keyManagers must be an array")
	}

	issuers := make([]string, 0, len(keyManagersList))
	keyManagers := make(map[string]string, len(keyManagersList))
	for _, km := range keyManagersList {
		kmMap, ok := km.(map[string]any)
		if !ok {
			return nil, nil, fmt.Errorf("invalid policy configuration: keyManagers entries must be objects")
		}

		name := strings.TrimSpace(getString(kmMap["name"]))
		issuer := strings.TrimSpace(getString(kmMap["issuer"]))
		if name == "" || issuer == "" {
			return nil, nil, fmt.Errorf("invalid policy configuration: each keyManager requires non-empty name and issuer")
		}

		issuers = append(issuers, issuer)
		keyManagers[name] = issuer
	}

	return issuers, keyManagers, nil
}

func isWellKnownEndpointRequest(path string) bool {
	return path == WellKnownEndpointPath || strings.HasSuffix(path, WellKnownEndpointPath)
}

// isMcpEndpointRequest reports whether the request targets the MCP endpoint (/mcp).
func isMcpEndpointRequest(operationPath string) bool {
	return strings.Contains(operationPath, McpPathSegment)
}

// isMcpPostRequest reports whether the request is the JSON-RPC POST to the MCP
// endpoint, the only MCP request carrying a body the exception lists can key off.
func isMcpPostRequest(method, operationPath string) bool {
	return strings.EqualFold(method, "POST") && isMcpEndpointRequest(operationPath)
}

// requiresTransportAuth reports whether a non-POST request to the MCP endpoint must
// be authenticated in the header phase. GET /mcp (SSE stream) and DELETE /mcp
// (session termination) carry no JSON-RPC payload, so the body phase cannot gate
// them. CORS preflights and the protected resource metadata endpoint stay public.
func (p *McpAuthPolicy) requiresTransportAuth(method, operationPath string) bool {
	if !isMcpEndpointRequest(operationPath) || isWellKnownEndpointRequest(operationPath) {
		return false
	}
	if isMcpPostRequest(method, operationPath) || strings.EqualFold(method, "OPTIONS") {
		return false
	}
	// No JSON-RPC name to match against the exception lists, so authenticate unless
	// the whole MCP server is unprotected.
	return !p.AuthConfig.isFullyUnprotected()
}

// isFullyUnprotected reports whether every capability group is disabled with no
// exceptions. An exception under a disabled group inverts to "requires auth", so a
// disabled group with exceptions still counts as protected.
func (c McpAuthConfig) isFullyUnprotected() bool {
	for _, config := range []SecurityConfig{c.Tools, c.Resources, c.Prompts, c.Methods} {
		if config.Enabled || len(config.Exceptions) > 0 {
			return false
		}
	}
	return true
}

func validateAuthFailureConfig(statusCode int, format string) error {
	if statusCode != 401 && statusCode != 403 {
		return fmt.Errorf("invalid policy configuration: onFailureStatusCode must be 401 or 403")
	}

	switch format {
	case "json", "plain", "minimal":
		return nil
	default:
		return fmt.Errorf("invalid policy configuration: errorMessageFormat must be one of [json, plain, minimal]")
	}
}

func buildInvalidConfigResponse(message string) policy.RequestAction {
	body, _ := json.Marshal(map[string]string{
		"error":   "Internal Server Error",
		"message": message,
	})
	return policy.ImmediateResponse{
		StatusCode: 500,
		Headers: map[string]string{
			"content-type": "application/json",
		},
		Body: body,
	}
}

func ensureRequestMetadata(reqCtx *policy.RequestContext) {
	if reqCtx.SharedContext == nil {
		reqCtx.SharedContext = &policy.SharedContext{}
	}
	if reqCtx.Metadata == nil {
		reqCtx.Metadata = map[string]any{}
	}
}

// ensureHeaderRequestMetadata mirrors ensureRequestMetadata for the header-phase
// context, so the response phase can rely on the shared metadata map existing.
func ensureHeaderRequestMetadata(reqCtx *policy.RequestHeaderContext) {
	if reqCtx.SharedContext == nil {
		reqCtx.SharedContext = &policy.SharedContext{}
	}
	if reqCtx.Metadata == nil {
		reqCtx.Metadata = map[string]any{}
	}
}

func (p *McpAuthPolicy) Mode() policy.ProcessingMode {
	return policy.ProcessingMode{
		RequestHeaderMode:  policy.HeaderModeProcess,
		RequestBodyMode:    policy.BodyModeBuffer,
		ResponseHeaderMode: policy.HeaderModeProcess,
		ResponseBodyMode:   policy.BodyModeSkip,
	}
}

func (p *McpAuthPolicy) OnRequestHeaders(ctx context.Context, reqCtx *policy.RequestHeaderContext, params map[string]interface{}) policy.RequestHeaderAction {
	if err := validateAuthFailureConfig(p.OnFailureStatusCode, p.ErrorMessageFormat); err != nil {
		v1r := buildInvalidConfigResponse(err.Error()).(policy.ImmediateResponse)
		return policy.ImmediateResponse{StatusCode: v1r.StatusCode, Headers: v1r.Headers, Body: v1r.Body}
	}
	ds := reqCtx.DownstreamRequest()

	// Stash the gateway's PRM pointer for the response phase, which has no vhost.
	ensureHeaderRequestMetadata(reqCtx)
	reqCtx.Metadata[MetadataKeyPrmPointer] = generateResourcePathFromFields(ds.Scheme, ds.Authority, reqCtx.Vhost, reqCtx.APIContext, params, WellKnownPath)

	// Check for GET /.well-known/oauth-protected-resource
	if ds.Method == "GET" && isWellKnownEndpointRequest(reqCtx.OperationPath) {
		slog.Debug("MCP Auth Policy: Handling well-known protected resource metadata request")
		sessionIds := reqCtx.DownstreamHeaders().Get(McpSessionHeader)
		sessionId := ""
		if len(sessionIds) > 0 {
			sessionId = sessionIds[0]
		}

		if p.PrmUpstreamURL != "" {
			if action, ok := p.servePassthroughPRM(ctx, reqCtx, ds, params, sessionId); ok {
				return action
			}
			// Fetch failed with no cached copy: fall through to the legacy
			// generated PRM below (never 5xx a discovery request if avoidable).
		}

		keyManagersRaw, ok := params["keyManagers"]
		if !ok {
			slog.Debug("MCP Auth Policy: Key managers not configured in params")
			return p.handleAuthFailure(reqCtx.SharedContext, p.OnFailureStatusCode, p.ErrorMessageFormat, "key managers not configured")
		}

		slog.Debug("MCP Auth Policy: Starting to parse key managers configuration")
		issuers, kms, err := parseKeyManagers(keyManagersRaw)
		if err != nil {
			v1r := buildInvalidConfigResponse(err.Error()).(policy.ImmediateResponse)
			return policy.ImmediateResponse{StatusCode: v1r.StatusCode, Headers: v1r.Headers, Body: v1r.Body}
		}
		if len(issuers) == 0 {
			return p.handleAuthFailure(reqCtx.SharedContext, p.OnFailureStatusCode, p.ErrorMessageFormat, "no valid key managers found")
		}

		if len(p.Issuers) > 0 {
			filteredIssuers := []string{}
			for _, ui := range p.Issuers {
				if issuer, ok := kms[ui]; ok {
					filteredIssuers = append(filteredIssuers, issuer)
					slog.Debug("MCP Auth Policy: Added issuer from user configuration", "issuer", issuer)
				}
			}
			issuers = filteredIssuers
		}

		if len(issuers) == 0 {
			return p.handleAuthFailure(reqCtx.SharedContext, p.OnFailureStatusCode, p.ErrorMessageFormat, "no matching issuers found")
		}

		// prmScopesSupported decouples what the PRM advertises from what
		// requiredScopes carries: an AS like Entra issues tokens whose scp claim
		// holds the short scope name while clients must request the full
		// resource-URI-qualified form.
		scopesSupported := p.RequiredScopes
		if len(p.PrmScopesSupported) > 0 {
			scopesSupported = p.PrmScopesSupported
		}
		prm := ProtectedResourceMetadata{
			Resource:             generateResourcePathFromFields(ds.Scheme, ds.Authority, reqCtx.Vhost, reqCtx.APIContext, params, "mcp"),
			AuthorizationServers: issuers,
			ScopesSupported:      scopesSupported,
		}
		jsonOut, _ := json.Marshal(prm)
		return policy.ImmediateResponse{
			StatusCode: 200,
			Headers: map[string]string{
				"Content-Type":   "application/json",
				McpSessionHeader: sessionId,
			},
			Body: jsonOut,
		}
	}

	// GET /mcp and DELETE /mcp carry no body, so gate them here instead of the body phase.
	if p.requiresTransportAuth(ds.Method, reqCtx.OperationPath) {
		slog.Debug("MCP Auth Policy: Authenticating MCP transport request",
			"method", ds.Method,
			"operationPath", reqCtx.OperationPath)
		return p.authenticate(ctx, reqCtx, params, p.RequiredScopes)
	}

	return policy.UpstreamRequestHeaderModifications{}
}

// servePassthroughPRM serves the upstream MCP server's own Protected Resource
// Metadata with only the `resource` field rewritten to the gateway's URL. The
// document is fetched from PrmUpstreamURL and cached for PrmCacheSeconds; on a
// fetch failure a stale cached copy is served. Returns ok=false only when no
// document is available at all, in which case the caller falls back to the
// legacy generated PRM.
func (p *McpAuthPolicy) servePassthroughPRM(ctx context.Context, reqCtx *policy.RequestHeaderContext, ds *policy.DownstreamRequest, params map[string]any, sessionId string) (policy.RequestHeaderAction, bool) {
	body, fromCache, err := p.fetchPRM(ctx)
	if err != nil {
		if body == nil {
			slog.Warn("MCP Auth Policy: PRM fetch failed with no cached copy, falling back to generated PRM",
				"url", p.PrmUpstreamURL, "error", err)
			return nil, false
		}
		slog.Warn("MCP Auth Policy: PRM fetch failed, serving stale cached copy",
			"url", p.PrmUpstreamURL, "error", err)
	}
	_ = fromCache

	var doc map[string]any
	if err := json.Unmarshal(body, &doc); err != nil {
		// The cache only ever holds validated JSON, so this should not happen.
		slog.Warn("MCP Auth Policy: cached PRM document is not valid JSON, falling back to generated PRM", "error", err)
		return nil, false
	}
	doc["resource"] = generateResourcePathFromFields(ds.Scheme, ds.Authority, reqCtx.Vhost, reqCtx.APIContext, params, "mcp")
	jsonOut, err := json.Marshal(doc)
	if err != nil {
		slog.Warn("MCP Auth Policy: failed to marshal rewritten PRM document, falling back to generated PRM", "error", err)
		return nil, false
	}
	return policy.ImmediateResponse{
		StatusCode: 200,
		Headers: map[string]string{
			"Content-Type":   "application/json",
			"Cache-Control":  fmt.Sprintf("max-age=%d", p.PrmCacheSeconds),
			McpSessionHeader: sessionId,
		},
		Body: jsonOut,
	}, true
}

// fetchPRM returns the upstream PRM document, serving from the instance cache
// while it is fresh. On a fetch error it returns the stale cached body (if any)
// alongside the error; the caller decides whether stale is acceptable. A non-2xx
// status or a non-JSON body counts as a fetch error and is never cached.
func (p *McpAuthPolicy) fetchPRM(ctx context.Context) (body []byte, fromCache bool, err error) {
	ttl := time.Duration(p.PrmCacheSeconds) * time.Second

	p.prmCache.mu.RLock()
	cached := p.prmCache.body
	age := time.Since(p.prmCache.fetchedAt)
	p.prmCache.mu.RUnlock()

	if cached != nil && age < ttl {
		return cached, true, nil
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, p.PrmUpstreamURL, nil)
	if err != nil {
		return cached, cached != nil, fmt.Errorf("building PRM request: %w", err)
	}
	req.Header.Set("Accept", "application/json")
	req.Header.Set("User-Agent", "wso2-mcp-auth-passthrough/0.3")

	resp, err := p.httpClient.Do(req)
	if err != nil {
		return cached, cached != nil, fmt.Errorf("fetching PRM: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return cached, cached != nil, fmt.Errorf("fetching PRM: upstream returned status %d", resp.StatusCode)
	}
	fresh, err := io.ReadAll(resp.Body)
	if err != nil {
		return cached, cached != nil, fmt.Errorf("reading PRM body: %w", err)
	}
	var probe map[string]any
	if err := json.Unmarshal(fresh, &probe); err != nil {
		return cached, cached != nil, fmt.Errorf("parsing PRM body: %w", err)
	}

	p.prmCache.mu.Lock()
	p.prmCache.body = fresh
	p.prmCache.fetchedAt = time.Now()
	p.prmCache.mu.Unlock()

	return fresh, false, nil
}

// resourceMetadataRe matches the resource_metadata challenge parameter; the name
// match is case-insensitive and the capture preserves the original name casing.
var resourceMetadataRe = regexp.MustCompile(`(?i)(resource_metadata=)"[^"]*"`)

// rewriteResourceMetadata replaces the value of a resource_metadata parameter in
// a WWW-Authenticate challenge with pointer, preserving every other parameter and
// their order. A Bearer challenge without the parameter gains it appended; any
// other challenge scheme is returned unchanged.
func rewriteResourceMetadata(v, pointer string) string {
	if resourceMetadataRe.MatchString(v) {
		return resourceMetadataRe.ReplaceAllString(v, `${1}"`+pointer+`"`)
	}
	if len(v) >= len("Bearer") && strings.EqualFold(v[:len("Bearer")], "Bearer") {
		return v + `, resource_metadata="` + pointer + `"`
	}
	return v
}

// pointerFrom returns the gateway's PRM pointer for the response phase: the value
// stashed by OnRequestHeaders when available, otherwise rebuilt from the
// downstream request snapshot plus APIContext and the gatewayHost parameter (the
// response phase has no vhost).
func (p *McpAuthPolicy) pointerFrom(respCtx *policy.ResponseHeaderContext, params map[string]any) string {
	if respCtx.SharedContext != nil && respCtx.Metadata != nil {
		if v, ok := respCtx.Metadata[MetadataKeyPrmPointer].(string); ok && v != "" {
			return v
		}
	}
	ds := respCtx.DownstreamRequest()
	apiContext := ""
	if respCtx.SharedContext != nil {
		apiContext = respCtx.APIContext
	}
	return generateResourcePathFromFields(ds.Scheme, ds.Authority, "", apiContext, params, WellKnownPath)
}

// OnResponseHeaders rewrites the resource_metadata pointer in an upstream 401
// WWW-Authenticate challenge so OAuth discovery resolves against the gateway
// rather than the upstream. Every other challenge parameter is preserved.
func (p *McpAuthPolicy) OnResponseHeaders(ctx context.Context, respCtx *policy.ResponseHeaderContext, params map[string]any) policy.ResponseHeaderAction {
	if !p.RewriteChallenge || respCtx.ResponseStatus != 401 {
		return policy.DownstreamResponseHeaderModifications{}
	}
	vals := respCtx.ResponseHeaders.Get(WWWAuthenticateHeader)
	if len(vals) == 0 {
		return policy.DownstreamResponseHeaderModifications{}
	}
	pointer := p.pointerFrom(respCtx, params)
	out := make([]string, 0, len(vals))
	for _, v := range vals {
		out = append(out, rewriteResourceMetadata(v, pointer))
	}
	return policy.DownstreamResponseHeaderModifications{
		HeadersToSet: map[string]string{WWWAuthenticateHeader: strings.Join(out, ", ")},
	}
}

// OnRequestBody processes the request body phase for MCP authentication.
func (p *McpAuthPolicy) OnRequestBody(ctx context.Context, reqCtx *policy.RequestContext, params map[string]any) policy.RequestAction {
	if err := validateAuthFailureConfig(p.OnFailureStatusCode, p.ErrorMessageFormat); err != nil {
		v1r := buildInvalidConfigResponse(err.Error()).(policy.ImmediateResponse)
		return policy.ImmediateResponse{StatusCode: v1r.StatusCode, Headers: v1r.Headers, Body: v1r.Body}
	}

	if p.GatewayHost != "" {
		ensureRequestMetadata(reqCtx)
		reqCtx.Metadata["gatewayHost"] = p.GatewayHost
	}

	ds := reqCtx.DownstreamRequest()
	if isMcpPostRequest(ds.Method, reqCtx.OperationPath) {
		if reqCtx.Body == nil || !reqCtx.Body.Present {
			return p.handleAuth(ctx, reqCtx, params, p.RequiredScopes)
		}
		var mcpReq MCPRequest
		if err := json.Unmarshal(reqCtx.Body.Content, &mcpReq); err != nil {
			slog.Debug("MCP Auth Policy: Failed to parse MCP request", "error", err)
			return p.handleBadRequest(err)
		}
		if err := validateUnambiguousMembers(reqCtx.Body.Content, mcpReq.Method); err != nil {
			slog.Debug("MCP Auth Policy: Rejecting MCP request with ambiguous member names", "error", err)
			return p.handleBadRequest(err)
		}

		slog.Debug("MCP Auth Policy: Extracted MCP attributes",
			"method", mcpReq.Method,
			"name", mcpReq.Params.Name,
			"uri", mcpReq.Params.URI)

		if !p.isAuthRequired(mcpReq) {
			slog.Debug("MCP Auth Policy: Skipping authentication for exempt request", "method", mcpReq.Method)
			return nil
		}

		return p.handleAuth(ctx, reqCtx, params, p.RequiredScopes)
	}

	return policy.UpstreamRequestModifications{}
}

// handleAuthFailure constructs an authentication failure response.
func (p *McpAuthPolicy) handleAuthFailure(shared *policy.SharedContext, statusCode int, format string, reason any) policy.ImmediateResponse {
	shared.AuthContext = &policy.AuthContext{
		Authenticated: false,
		AuthType:      AuthType,
		Previous:      shared.AuthContext,
	}
	if shared.Metadata == nil {
		shared.Metadata = map[string]any{}
	}
	shared.Metadata[MetadataKeyAuthSuccess] = false
	shared.Metadata[MetadataKeyAuthMethod] = "mcpAuth"

	headers := map[string]string{"content-type": "application/json"}
	var body string
	switch format {
	case "plain":
		body = fmt.Sprintf("Authentication failed: %s", reason)
		headers["content-type"] = "text/plain"
	case "minimal":
		body = "Unauthorized"
	default:
		errResponse := map[string]interface{}{
			"error":   "Unauthorized",
			"message": fmt.Sprintf("MCP authentication failed: %s", reason),
		}
		bodyBytes, _ := json.Marshal(errResponse)
		body = string(bodyBytes)
	}
	return policy.ImmediateResponse{
		StatusCode: statusCode,
		Headers:    headers,
		Body:       []byte(body),
	}
}

// handleBadRequest constructs an HTTP 400 response carrying a JSON-RPC 2.0 error
// object, as MCP requires for protocol-level errors (unlike authentication
// failures, which are signalled at the HTTP/OAuth layer with WWW-Authenticate).
// A body with invalid JSON syntax is reported as -32700 (Parse error); a body
// that is valid JSON but not a valid request object as -32600 (Invalid Request).
// The id is null because it cannot be recovered from an unparseable request. This
// response is not subject to errorMessageFormat, which governs auth failures only.
func (p *McpAuthPolicy) handleBadRequest(parseErr error) policy.ImmediateResponse {
	code, message, data := JSONRPCInvalidRequest, "Invalid Request", "Invalid MCP request format"
	var syntaxErr *json.SyntaxError
	var ambiguousErr *ambiguousMemberError
	switch {
	case errors.As(parseErr, &syntaxErr):
		code, message = JSONRPCParseError, "Parse error"
	case errors.As(parseErr, &ambiguousErr):
		data = fmt.Sprintf("Ambiguous MCP request: %s", ambiguousErr)
	}

	body, _ := json.Marshal(map[string]any{
		"jsonrpc": JSONRPCVersion,
		"id":      nil,
		"error": map[string]any{
			"code":    code,
			"message": message,
			"data":    data,
		},
	})

	return policy.ImmediateResponse{
		StatusCode: 400,
		Headers:    map[string]string{"content-type": "application/json"},
		Body:       body,
	}
}

// authenticate delegates token validation to the JWT Auth policy's header phase and
// adapts the outcome to MCP semantics: failures gain the WWW-Authenticate challenge
// and the session header, successes are re-stamped as mcp/oauth. Shared by both the
// header and body phases.
func (p *McpAuthPolicy) authenticate(ctx context.Context, headerCtx *policy.RequestHeaderContext, params map[string]any, scopes []string) policy.RequestHeaderAction {
	type requestHeaderPolicer interface {
		OnRequestHeaders(context.Context, *policy.RequestHeaderContext, map[string]interface{}) policy.RequestHeaderAction
	}

	ds := headerCtx.DownstreamRequest()
	sessionIds := headerCtx.DownstreamHeaders().Get(McpSessionHeader)
	sessionId := ""
	if len(sessionIds) > 0 {
		sessionId = sessionIds[0]
	}

	// Params are forwarded to the delegated policy verbatim, so any parameter it
	// already understands needs no mapping here — only an entry in this policy's
	// definition to make it configurable, since `parameters` sets
	// additionalProperties: false. forwardToken, forwardedTokenHeader,
	// forwardTokenStripScheme and userIdClaim reach jwt-auth this way and keep its
	// names, types and defaults deliberately: a divergence in either name or type
	// would be read as absent and silently fall back to jwt-auth's default.
	//
	// requiredScopes is the one parameter that must not be forwarded. mcp-auth
	// advertises it through protected resource metadata without enforcing it,
	// whereas jwt-auth would reject tokens that lack the listed scopes.
	jwtParams := maps.Clone(params)
	delete(jwtParams, "requiredScopes")

	// forwardToken is the one parameter whose default differs from jwt-auth's.
	// MCP servers are frequently third parties, so this policy defaults to not
	// releasing the client credential upstream. Resolving it here rather than
	// leaving the key absent is what makes that default authoritative: an absent
	// key would fall through to jwt-auth's own default of true, whatever the
	// control plane does or does not materialise from the policy definition.
	forwardToken := getBoolParam(params, "forwardToken", false)
	jwtParams["forwardToken"] = forwardToken

	slog.Debug("MCP Auth Policy: Delegating authentication to JWT Auth Policy")
	jwtPolicy, err := jwtauth.GetPolicy(policy.PolicyMetadata{}, jwtParams)
	if err != nil {
		return p.handleAuthFailure(headerCtx.SharedContext, 500, "json", fmt.Sprintf("jwtauth.GetPolicy unavailable: %s", err))
	}
	hrp, ok := jwtPolicy.(requestHeaderPolicer)
	if !ok {
		return p.handleAuthFailure(headerCtx.SharedContext, 500, "json", "jwtPolicy does not implement OnRequestHeaders")
	}

	headerAction := hrp.OnRequestHeaders(ctx, headerCtx, jwtParams)
	if ir, ok := headerAction.(policy.ImmediateResponse); ok {
		slog.Debug("MCP Auth Policy: Authentication failed in JWT Auth Policy, handling failure")
		headerCtx.SharedContext.AuthContext = &policy.AuthContext{
			Authenticated: false,
			AuthType:      AuthType,
			Previous:      headerCtx.SharedContext.AuthContext,
		}
		headers := ir.Headers
		body := ir.Body
		escapedDesc := ""
		var errResp map[string]any
		isJSON := headers["content-type"] == "application/json"
		if isJSON {
			if err := json.Unmarshal(ir.Body, &errResp); err == nil {
				if errDesc, ok := errResp["message"].(string); ok {
					escapedDesc = strings.ReplaceAll(errDesc, "\"", "'")
				}
			} else {
				errResp = nil
			}
		}
		switch p.GatewayChallengeMode {
		case GatewayChallengeModeLegacy:
			headers[WWWAuthenticateHeader] = generateWwwAuthenticateHeaderFromFields(ds.Scheme, ds.Authority, headerCtx.Vhost, headerCtx.APIContext, params, scopes, escapedDesc)
		case GatewayChallengeModeNone:
			// no WWW-Authenticate at all
		default: // GatewayChallengeModeGateway, and the zero value for instances not built via GetPolicy
			tokenHeaderName := getStringParam(params, "headerName", "Authorization")
			tokenPresent := len(headerCtx.DownstreamHeaders().Get(tokenHeaderName)) > 0
			headers[WWWAuthenticateHeader] = generateGatewayChallenge(p.GatewayChallengeRealm, tokenHeaderName, tokenPresent, escapedDesc)
			// Make the body say the same thing, so a client that only shows the
			// body (Claude Code does) still learns which header the gateway wants.
			if errResp != nil {
				errResp["message"] = gatewayFailureMessage(tokenHeaderName, tokenPresent, escapedDesc)
				if b, err := json.Marshal(errResp); err == nil {
					body = b
				}
			}
		}
		headers[McpSessionHeader] = sessionId
		return policy.ImmediateResponse{
			StatusCode: ir.StatusCode,
			Headers:    headers,
			Body:       body,
		}
	}
	// Override AuthType to mcp/oauth: mcp-auth is the effective policy that ran
	if headerCtx.SharedContext.AuthContext != nil {
		headerCtx.SharedContext.AuthContext.AuthType = AuthType
	}
	return headerAction
}

// handleAuth performs MCP authentication in the request body phase, translating the
// shared delegation's header-phase action into a body-phase one.
func (p *McpAuthPolicy) handleAuth(ctx context.Context, reqCtx *policy.RequestContext, params map[string]any, scopes []string) policy.RequestAction {
	ds := reqCtx.DownstreamRequest()
	headerCtx := &policy.RequestHeaderContext{
		SharedContext: reqCtx.SharedContext,
		Headers:       reqCtx.Headers,
		Path:          ds.Path,
		Method:        ds.Method,
		Authority:     ds.Authority,
		Scheme:        ds.Scheme,
		Vhost:         reqCtx.Vhost,
		// Propagate the downstream snapshot so the delegated JWT auth
		// policy validates the Authorization header the client actually sent,
		// not one a peer policy rewrote during the header phase.
		Downstream: reqCtx.Downstream,
	}

	forwardToken := getBoolParam(params, "forwardToken", false)

	switch a := p.authenticate(ctx, headerCtx, params, scopes).(type) {
	case policy.ImmediateResponse:
		return a
	case policy.UpstreamRequestHeaderModifications:
		tokenHeader := getStringParam(params, "headerName", "Authorization")
		if isTokenHeaderClaimed(reqCtx.Downstream, reqCtx.Headers, tokenHeader) {
			slog.Debug("MCP Auth Policy: Inbound token header claimed by a peer policy, preserving it",
				"headerName", tokenHeader)

			forwardedTokenHeader := getStringParam(params, "forwardedTokenHeader", "x-forwarded-authorization")
			if forwardToken && strings.EqualFold(forwardedTokenHeader, tokenHeader) {
				slog.Warn("MCP Auth Policy: forwardedTokenHeader is claimed by another policy, so the validated token is not forwarded upstream; "+
					"set forwardedTokenHeader to a header no other policy writes",
					"forwardedTokenHeader", forwardedTokenHeader,
					"headerName", tokenHeader)
			}

			a = preserveTokenHeader(a, tokenHeader)
		}
		return policy.UpstreamRequestModifications{
			HeadersToSet:            a.HeadersToSet,
			HeadersToRemove:         a.HeadersToRemove,
			UpstreamName:            a.UpstreamName,
			Path:                    a.Path,
			Method:                  a.Method,
			QueryParametersToAdd:    a.QueryParametersToAdd,
			QueryParametersToRemove: a.QueryParametersToRemove,
			AnalyticsMetadata:       a.AnalyticsMetadata,
			DynamicMetadata:         a.DynamicMetadata,
			AnalyticsHeaderFilter:   a.AnalyticsHeaderFilter,
		}
	}
	return nil
}

// generateResourcePathFromFields builds the resource URL from individual context fields
// instead of a full RequestContext, enabling use in both header and body phases.
func generateResourcePathFromFields(scheme, authority, vhost, apiContext string, params map[string]any, resource string) string {
	_, port := parseAuthority(authority)

	var host string
	if vhost != "" && !strings.Contains(vhost, "*") {
		host = vhost
	} else {
		host = getStringParam(params, "gatewayHost", "localhost")
	}

	if port == -1 {
		if scheme == "https" {
			port = 8443
		} else {
			port = 8080
		}
	}

	hostWithPort := host
	if !isStandardPort(scheme, port) {
		hostWithPort = fmt.Sprintf("%s:%d", host, port)
	}

	if apiContext != "" {
		return fmt.Sprintf("%s://%s%s/%s", scheme, hostWithPort, apiContext, resource)
	}
	return fmt.Sprintf("%s://%s/%s", scheme, hostWithPort, resource)
}

// generateGatewayChallenge builds the gateway's own WWW-Authenticate challenge for
// mode "gateway": a Bearer challenge scoped by realm, deliberately WITHOUT a
// resource_metadata pointer (the PRM describes the upstream MCP server's
// authorization server, not the gateway's). Per RFC 6750 §3.1 the error code
// is only set when a credential was present and rejected; when the gateway
// token header is absent, only an error_description naming the expected header
// is emitted so that a human reading the response knows what to send.
func generateGatewayChallenge(realm, tokenHeaderName string, tokenPresent bool, errorDesc string) string {
	if realm == "" {
		realm = DefaultGatewayChallengeRealm
	}
	realm = strings.ReplaceAll(realm, "\"", "'")
	headerValue := "Bearer realm=\"" + realm + "\""
	if tokenPresent {
		headerValue += ", error=\"invalid_token\""
	}
	headerValue += ", error_description=\"" + strings.ReplaceAll(gatewayFailureMessage(tokenHeaderName, tokenPresent, errorDesc), "\"", "'") + "\""
	return headerValue
}

// gatewayFailureMessage is the human-readable explanation shared by the
// "gateway" mode challenge and the JSON body.
func gatewayFailureMessage(tokenHeaderName string, tokenPresent bool, errorDesc string) string {
	var msg string
	if tokenPresent {
		msg = fmt.Sprintf("Gateway rejected the token in the '%s' header", tokenHeaderName)
		if errorDesc != "" {
			msg += ": " + errorDesc
		}
	} else {
		msg = fmt.Sprintf("Gateway credential missing: send the gateway token as '%s: Bearer <token>'", tokenHeaderName)
	}
	return msg + ". This challenge is from the gateway, not the MCP server."
}

// generateWwwAuthenticateHeaderFromFields builds the WWW-Authenticate header from individual context fields.
func generateWwwAuthenticateHeaderFromFields(scheme, authority, vhost, apiContext string, params map[string]any, scopes []string, errorDesc string) string {
	headerValue := AuthMethodBearer + "\"" + generateResourcePathFromFields(scheme, authority, vhost, apiContext, params, WellKnownPath) + "\""
	if len(scopes) > 0 {
		headerValue += ", scope=\"" + strings.Join(scopes, " ") + "\""
	}
	if errorDesc != "" {
		headerValue += ", error=\"invalid_token\", error_description=\"" + errorDesc + "\""
	}
	return headerValue
}
