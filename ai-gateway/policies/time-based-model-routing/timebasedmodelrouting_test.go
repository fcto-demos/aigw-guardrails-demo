/*
 * Copyright (c) 2026, WSO2 LLC. (https://www.wso2.com).
 * Licensed under the Apache License, Version 2.0.
 */

package timebasedmodelrouting

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	policy "github.com/wso2/api-platform/sdk/core/policy/v1alpha2"
)

func testParams() map[string]interface{} {
	return map[string]interface{}{
		"timezone": "Asia/Colombo",
		"schedules": []interface{}{
			map[string]interface{}{
				"name": "morning",
				"from": "06:00",
				"to":   "12:00",
				"model": map[string]interface{}{
					"modelName":    "morning-model",
					"providerName": "provider-a",
				},
			},
			map[string]interface{}{
				"name": "evening",
				"from": "18:00",
				"to":   "23:00",
				"model": map[string]interface{}{
					"modelName": "evening-model",
				},
			},
		},
		"fallback": map[string]interface{}{
			"modelName":    "default-model",
			"providerName": "provider-default",
		},
		"requestModel": map[string]interface{}{
			"location":   "payload",
			"identifier": "$.model",
		},
	}
}

func requestContext(body string) *policy.RequestContext {
	return &policy.RequestContext{
		SharedContext: &policy.SharedContext{Metadata: map[string]interface{}{}},
		Body:          &policy.Body{Content: []byte(body)},
		Path:          "/v1/chat/completions",
	}
}

func fixedNow(t *testing.T, value string) {
	t.Helper()
	parsed, err := time.Parse(time.RFC3339, value)
	if err != nil {
		t.Fatal(err)
	}
	previous := nowFunc
	nowFunc = func() time.Time { return parsed }
	t.Cleanup(func() { nowFunc = previous })
}

func TestProcessingModeBuffersOnlyRequestBody(t *testing.T) {
	raw, err := GetPolicy(policy.PolicyMetadata{}, testParams())
	if err != nil {
		t.Fatal(err)
	}
	mode := raw.(*TimeBasedModelRoutingPolicy).Mode()
	if mode.RequestBodyMode != policy.BodyModeBuffer ||
		mode.ResponseHeaderMode != policy.HeaderModeSkip ||
		mode.ResponseBodyMode != policy.BodyModeSkip {
		t.Fatalf("time routing must process only the request body: %#v", mode)
	}
}

func TestRoutesByConfiguredTimezone(t *testing.T) {
	fixedNow(t, "2026-08-30T01:00:00Z") // 06:30 in Asia/Colombo.
	raw, err := GetPolicy(policy.PolicyMetadata{}, testParams())
	if err != nil {
		t.Fatal(err)
	}
	ctx := requestContext(`{"model":"client-model","messages":[{"role":"user","content":"hello"}]}`)
	action := raw.(*TimeBasedModelRoutingPolicy).OnRequestBody(context.Background(), ctx, nil)
	mods, ok := action.(policy.UpstreamRequestModifications)
	if !ok {
		t.Fatalf("expected modifications, got %T", action)
	}
	var payload map[string]interface{}
	if err := json.Unmarshal(mods.Body, &payload); err != nil {
		t.Fatal(err)
	}
	if payload["model"] != "morning-model" {
		t.Fatalf("model = %v, want morning-model", payload["model"])
	}
	if mods.UpstreamName == nil || *mods.UpstreamName != "provider-a" {
		t.Fatalf("upstream = %v, want provider-a", mods.UpstreamName)
	}
	if ctx.Metadata[metadataSelectedRoute] != "morning" {
		t.Fatalf("route metadata = %v, want morning", ctx.Metadata[metadataSelectedRoute])
	}
	if ctx.Metadata[metadataProviderRouting] != "provider-a" {
		t.Fatalf("provider routing metadata = %v, want provider-a", ctx.Metadata[metadataProviderRouting])
	}
}

func TestRoutingInitializesMissingSharedContext(t *testing.T) {
	fixedNow(t, "2026-08-30T01:00:00Z")
	raw, err := GetPolicy(policy.PolicyMetadata{}, testParams())
	if err != nil {
		t.Fatal(err)
	}
	ctx := requestContext(`{"model":"client-model"}`)
	ctx.SharedContext = nil
	action := raw.(*TimeBasedModelRoutingPolicy).OnRequestBody(context.Background(), ctx, nil)
	if _, ok := action.(policy.UpstreamRequestModifications); !ok {
		t.Fatalf("expected modifications, got %T", action)
	}
	if ctx.SharedContext == nil || ctx.Metadata[metadataSelectedModel] != "morning-model" {
		t.Fatalf("routing metadata was not initialized: %#v", ctx.SharedContext)
	}
	if ctx.Metadata[metadataProviderRouting] != "provider-a" {
		t.Fatalf("provider routing metadata = %v, want provider-a", ctx.Metadata[metadataProviderRouting])
	}
}

func TestNoMatchUsesDefaultOrPreservesOriginal(t *testing.T) {
	fixedNow(t, "2026-08-30T08:00:00Z") // 13:30 in Asia/Colombo.
	raw, err := GetPolicy(policy.PolicyMetadata{}, testParams())
	if err != nil {
		t.Fatal(err)
	}
	ctx := requestContext(`{"model":"client-model"}`)
	mods := raw.(*TimeBasedModelRoutingPolicy).OnRequestBody(context.Background(), ctx, nil).(policy.UpstreamRequestModifications)
	var payload map[string]interface{}
	if err := json.Unmarshal(mods.Body, &payload); err != nil {
		t.Fatal(err)
	}
	if payload["model"] != "default-model" || mods.UpstreamName == nil || *mods.UpstreamName != "provider-default" {
		t.Fatalf("default was not applied: payload=%v upstream=%v", payload, mods.UpstreamName)
	}

	params := testParams()
	delete(params, "fallback")
	raw, err = GetPolicy(policy.PolicyMetadata{}, params)
	if err != nil {
		t.Fatal(err)
	}
	mods = raw.(*TimeBasedModelRoutingPolicy).OnRequestBody(context.Background(), requestContext(`{"model":"client-model"}`), nil).(policy.UpstreamRequestModifications)
	if mods.Body != nil || mods.UpstreamName != nil {
		t.Fatalf("expected unchanged request, got %#v", mods)
	}
}

func TestOvernightScheduleMatchesAcrossMidnight(t *testing.T) {
	params := testParams()
	params["schedules"] = []interface{}{
		map[string]interface{}{
			"name": "night",
			"from": "22:00",
			"to":   "06:00",
			"model": map[string]interface{}{
				"modelName": "night-model",
			},
		},
	}
	raw, err := GetPolicy(policy.PolicyMetadata{}, params)
	if err != nil {
		t.Fatal(err)
	}

	for _, now := range []string{"2026-08-30T18:00:00Z", "2026-08-30T00:00:00Z"} {
		t.Run(now, func(t *testing.T) {
			fixedNow(t, now)
			ctx := requestContext(`{"model":"client-model"}`)
			mods := raw.(*TimeBasedModelRoutingPolicy).OnRequestBody(context.Background(), ctx, nil).(policy.UpstreamRequestModifications)
			var payload map[string]interface{}
			if err := json.Unmarshal(mods.Body, &payload); err != nil {
				t.Fatal(err)
			}
			if payload["model"] != "night-model" {
				t.Fatalf("model = %v, want night-model", payload["model"])
			}
		})
	}
}

func TestOvernightScheduleWithDaysUsesStartDay(t *testing.T) {
	params := testParams()
	params["schedules"] = []interface{}{
		map[string]interface{}{
			"name": "saturday-night",
			"from": "22:00",
			"to":   "06:00",
			"days": []interface{}{"Sat"},
			"model": map[string]interface{}{
				"modelName": "saturday-night-model",
			},
		},
	}
	raw, err := GetPolicy(policy.PolicyMetadata{}, params)
	if err != nil {
		t.Fatal(err)
	}

	fixedNow(t, "2026-08-29T20:00:00Z") // Sunday 01:30 in Asia/Colombo, from Saturday's window.
	ctx := requestContext(`{"model":"client-model"}`)
	mods := raw.(*TimeBasedModelRoutingPolicy).OnRequestBody(context.Background(), ctx, nil).(policy.UpstreamRequestModifications)
	var payload map[string]interface{}
	if err := json.Unmarshal(mods.Body, &payload); err != nil {
		t.Fatal(err)
	}
	if payload["model"] != "saturday-night-model" {
		t.Fatalf("model = %v, want saturday-night-model", payload["model"])
	}

	fixedNow(t, "2026-08-30T20:00:00Z") // Monday 01:30 in Asia/Colombo, not Saturday's window.
	ctx = requestContext(`{"model":"client-model"}`)
	mods = raw.(*TimeBasedModelRoutingPolicy).OnRequestBody(context.Background(), ctx, nil).(policy.UpstreamRequestModifications)
	if err := json.Unmarshal(mods.Body, &payload); err != nil {
		t.Fatal(err)
	}
	if payload["model"] != "default-model" {
		t.Fatalf("model = %v, want default-model", payload["model"])
	}
}

func TestDaysRestrictSchedule(t *testing.T) {
	params := testParams()
	params["schedules"] = []interface{}{
		map[string]interface{}{
			"name": "weekday",
			"from": "09:00",
			"to":   "17:00",
			"days": []interface{}{"Mon", "Tue", "Wed", "Thu", "Fri"},
			"model": map[string]interface{}{
				"modelName": "weekday-model",
			},
		},
	}
	raw, err := GetPolicy(policy.PolicyMetadata{}, params)
	if err != nil {
		t.Fatal(err)
	}

	fixedNow(t, "2026-08-31T05:00:00Z") // Monday 10:30 in Asia/Colombo.
	ctx := requestContext(`{"model":"client-model"}`)
	mods := raw.(*TimeBasedModelRoutingPolicy).OnRequestBody(context.Background(), ctx, nil).(policy.UpstreamRequestModifications)
	var payload map[string]interface{}
	if err := json.Unmarshal(mods.Body, &payload); err != nil {
		t.Fatal(err)
	}
	if payload["model"] != "weekday-model" {
		t.Fatalf("model = %v, want weekday-model", payload["model"])
	}

	fixedNow(t, "2026-08-30T05:00:00Z") // Sunday 10:30 in Asia/Colombo.
	ctx = requestContext(`{"model":"client-model"}`)
	mods = raw.(*TimeBasedModelRoutingPolicy).OnRequestBody(context.Background(), ctx, nil).(policy.UpstreamRequestModifications)
	if err := json.Unmarshal(mods.Body, &payload); err != nil {
		t.Fatal(err)
	}
	if payload["model"] != "default-model" {
		t.Fatalf("model = %v, want default-model", payload["model"])
	}
}

func TestRejectsOverlappingSchedules(t *testing.T) {
	params := testParams()
	params["schedules"] = []interface{}{
		map[string]interface{}{
			"from":  "09:00",
			"to":    "12:00",
			"model": map[string]interface{}{"modelName": "a"},
		},
		map[string]interface{}{
			"from":  "11:00",
			"to":    "13:00",
			"model": map[string]interface{}{"modelName": "b"},
		},
	}
	if _, err := GetPolicy(policy.PolicyMetadata{}, params); err == nil {
		t.Fatal("expected overlapping schedule error")
	}
}

func TestSameTimesAreRejected(t *testing.T) {
	params := testParams()
	params["schedules"] = []interface{}{
		map[string]interface{}{
			"from":  "09:00",
			"to":    "09:00",
			"model": map[string]interface{}{"modelName": "a"},
		},
	}
	if _, err := GetPolicy(policy.PolicyMetadata{}, params); err == nil || !strings.Contains(err.Error(), "same from and to") {
		t.Fatalf("expected same time error, got %v", err)
	}
}

func TestRewritesEveryRequestModelLocation(t *testing.T) {
	fixedNow(t, "2026-08-30T01:00:00Z")
	tests := []struct {
		name       string
		location   string
		identifier string
		path       string
		assert     func(*testing.T, policy.UpstreamRequestModifications)
	}{
		{
			name: "header", location: "header", identifier: "x-model", path: "/invoke",
			assert: func(t *testing.T, mods policy.UpstreamRequestModifications) {
				if mods.HeadersToSet["x-model"] != "morning-model" {
					t.Fatalf("headers = %#v", mods.HeadersToSet)
				}
			},
		},
		{
			name: "query parameter", location: "queryParam", identifier: "model", path: "/invoke?model=client&x=1",
			assert: func(t *testing.T, mods policy.UpstreamRequestModifications) {
				if mods.Path == nil || *mods.Path != "/invoke?model=morning-model&x=1" {
					t.Fatalf("path = %v", mods.Path)
				}
			},
		},
		{
			name: "capture-group path", location: "pathParam", identifier: `model/([A-Za-z0-9.:-]+)/`, path: "/model/client-model/invoke",
			assert: func(t *testing.T, mods policy.UpstreamRequestModifications) {
				if mods.Path == nil || *mods.Path != "/model/morning-model/invoke" {
					t.Fatalf("path = %v", mods.Path)
				}
			},
		},
		{
			name: "Gemini lookbehind path", location: "pathParam", identifier: `(?<=models/)[a-zA-Z0-9.\-]+`, path: "/v1beta/models/gemini-old:generateContent",
			assert: func(t *testing.T, mods policy.UpstreamRequestModifications) {
				if mods.Path == nil || *mods.Path != "/v1beta/models/morning-model:generateContent" {
					t.Fatalf("path = %v", mods.Path)
				}
			},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			params := testParams()
			params["requestModel"] = map[string]interface{}{
				"location": tt.location, "identifier": tt.identifier,
			}
			raw, err := GetPolicy(policy.PolicyMetadata{}, params)
			if err != nil {
				t.Fatal(err)
			}
			ctx := requestContext(`{"model":"client-model","prompt":"short"}`)
			ctx.Path = tt.path
			action := raw.(*TimeBasedModelRoutingPolicy).OnRequestBody(context.Background(), ctx, nil)
			mods, ok := action.(policy.UpstreamRequestModifications)
			if !ok {
				t.Fatalf("expected modifications, got %#v", action)
			}
			tt.assert(t, mods)
		})
	}
}

func TestQueryRewriteFailureDoesNotPublishRoutingMetadata(t *testing.T) {
	fixedNow(t, "2026-08-30T01:00:00Z")
	params := testParams()
	params["requestModel"] = map[string]interface{}{
		"location": "queryParam", "identifier": "model",
	}
	raw, err := GetPolicy(policy.PolicyMetadata{}, params)
	if err != nil {
		t.Fatal(err)
	}
	ctx := requestContext(`{"model":"client-model"}`)
	ctx.Path = "/invoke?model=%zz"
	action := raw.(*TimeBasedModelRoutingPolicy).OnRequestBody(context.Background(), ctx, nil)
	response, ok := action.(policy.ImmediateResponse)
	if !ok || response.StatusCode != 400 {
		t.Fatalf("expected 400 response, got %#v", action)
	}
	if _, exists := ctx.Metadata[metadataSelectedModel]; exists {
		t.Fatalf("routing metadata must not be published after rewrite failure: %#v", ctx.Metadata)
	}
}

func TestCompilePathModelExpressionHandlesNestedLookbehindGroups(t *testing.T) {
	compiled, group, err := compilePathModelExpression(`(?<=(models|tunedModels)/)[a-z.-]+`)
	if err != nil {
		t.Fatal(err)
	}
	path := "/v1/tunedModels/client-model:generate"
	rewritten, ok := rewritePathParameter(path, compiled, group, "routed-model")
	if !ok {
		t.Fatal("expected nested lookbehind expression to match")
	}
	if rewritten != "/v1/tunedModels/routed-model:generate" {
		t.Fatalf("rewritten path = %q", rewritten)
	}
}

func TestCompilePathModelExpressionRejectsUnbalancedLookbehind(t *testing.T) {
	_, _, err := compilePathModelExpression(`(?<=(models|tunedModels)/[a-z.-]+`)
	if err == nil || !strings.Contains(err.Error(), "unterminated positive lookbehind") {
		t.Fatalf("error = %v, want unterminated positive lookbehind", err)
	}
}

func selectedDaySwitches(selected ...time.Weekday) map[string]interface{} {
	days := make(map[string]interface{}, 7)
	for day := time.Sunday; day <= time.Saturday; day++ {
		days[day.String()] = false
	}
	for _, day := range selected {
		days[day.String()] = true
	}
	return days
}

func TestDaySwitchesRouteOnlyOnMondayAndWednesday(t *testing.T) {
	for _, window := range []struct {
		name, from, to string
		hour           int
	}{
		{"same-day", "09:00", "17:00", 10},
		{"overnight-start", "22:00", "06:00", 23},
		{"overnight-next-day", "22:00", "06:00", 25},
	} {
		t.Run(window.name, func(t *testing.T) {
			for offset := 0; offset < 7; offset++ {
				startDay := time.Date(2026, 9, 7+offset, 0, 0, 0, 0, time.UTC)
				t.Run(startDay.Weekday().String(), func(t *testing.T) {
					params := testParams()
					params["timezone"] = "UTC"
					params["schedules"] = []interface{}{map[string]interface{}{
						"from": window.from, "to": window.to,
						"days":  selectedDaySwitches(time.Monday, time.Wednesday),
						"model": map[string]interface{}{"modelName": "selected-day-model"},
					}}
					raw, err := GetPolicy(policy.PolicyMetadata{}, params)
					if err != nil {
						t.Fatal(err)
					}
					fixedNow(t, startDay.Add(time.Duration(window.hour)*time.Hour).Format(time.RFC3339))
					ctx := requestContext(`{"model":"client-model"}`)
					mods := raw.(*TimeBasedModelRoutingPolicy).OnRequestBody(context.Background(), ctx, nil).(policy.UpstreamRequestModifications)
					var payload map[string]interface{}
					if err := json.Unmarshal(mods.Body, &payload); err != nil {
						t.Fatal(err)
					}
					want := "default-model"
					if startDay.Weekday() == time.Monday || startDay.Weekday() == time.Wednesday {
						want = "selected-day-model"
					}
					if payload["model"] != want {
						t.Fatalf("model = %v, want %s", payload["model"], want)
					}
				})
			}
		})
	}
}

func TestDaySwitchValidation(t *testing.T) {
	for _, tt := range []struct {
		name      string
		days      interface{}
		wantError string
	}{
		{"all off", selectedDaySwitches(), "must select at least one day"},
		{"non-boolean", map[string]interface{}{"Monday": "true"}, "must be a boolean"},
		{"unknown day", map[string]interface{}{"Funday": true}, "full weekday name"},
		{"abbreviated switch", map[string]interface{}{"Mon": true}, "full weekday name"},
		{"invalid type", "Monday", "object of weekday booleans"},
		{"legacy duplicate", []interface{}{"Mon", "Monday"}, "duplicates"},
		{"empty legacy list", []interface{}{}, "non-empty array"},
	} {
		t.Run(tt.name, func(t *testing.T) {
			params := testParams()
			params["schedules"].([]interface{})[0].(map[string]interface{})["days"] = tt.days
			if _, err := parseConfig(params); err == nil || !strings.Contains(err.Error(), tt.wantError) {
				t.Fatalf("error = %v, want %q", err, tt.wantError)
			}
		})
	}
}

func TestDaySwitchDefaultsAndLegacyLists(t *testing.T) {
	for _, tt := range []struct {
		name string
		days interface{}
		want []time.Weekday
	}{
		{"omitted", nil, []time.Weekday{0, 1, 2, 3, 4, 5, 6}},
		{"empty object", map[string]interface{}{}, []time.Weekday{0, 1, 2, 3, 4, 5, 6}},
		{"omitted switches enabled", map[string]interface{}{"Saturday": false, "Sunday": false}, []time.Weekday{1, 2, 3, 4, 5}},
		{"all enabled", selectedDaySwitches(0, 1, 2, 3, 4, 5, 6), []time.Weekday{0, 1, 2, 3, 4, 5, 6}},
		{"legacy list", []interface{}{"Mon", "Wednesday"}, []time.Weekday{1, 3}},
	} {
		t.Run(tt.name, func(t *testing.T) {
			params := testParams()
			if tt.days != nil {
				params["schedules"].([]interface{})[0].(map[string]interface{})["days"] = tt.days
			}
			cfg, err := parseConfig(params)
			if err != nil {
				t.Fatal(err)
			}
			for day := time.Sunday; day <= time.Saturday; day++ {
				want := false
				for _, selected := range tt.want {
					want = want || day == selected
				}
				if got := scheduleStartAppliesOn(cfg.Schedules[0], day); got != want {
					t.Fatalf("%s enabled = %v, want %v", day, got, want)
				}
			}
		})
	}
}

func TestDaySwitchOverlapValidation(t *testing.T) {
	params := testParams()
	items := params["schedules"].([]interface{})
	first := items[0].(map[string]interface{})
	second := items[1].(map[string]interface{})
	first["days"] = selectedDaySwitches(time.Monday)
	second["from"], second["to"] = first["from"], first["to"]
	second["days"] = selectedDaySwitches(time.Wednesday)
	if _, err := parseConfig(params); err != nil {
		t.Fatalf("disjoint selected days must not overlap: %v", err)
	}
	second["days"] = selectedDaySwitches(time.Monday, time.Wednesday)
	if _, err := parseConfig(params); err == nil || !strings.Contains(err.Error(), "overlaps") {
		t.Fatalf("expected overlap on Monday, got %v", err)
	}
}
