/*
 *  Copyright (c) 2026, WSO2 LLC. (http://www.wso2.com) All Rights Reserved.
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

package mcpauthn

import (
	"net/http"
	"slices"

	policy "github.com/wso2/api-platform/sdk/core/policy/v1alpha2"
)

// isTokenHeaderClaimed reports whether another peer policy has taken ownership
// of the inbound token header by replacing the client-supplied value
//
// If the original header value cannot be retrieved from the request snapshot,
// the header is treated as unclaimed to preserve the existing behaviour of
// stripping it. This avoids the risk of forwarding client credentials when the
// ownership cannot be determined. The absent-value check also handles legacy
// gateways where the request snapshot may be missing or unpopulated in
// different forms.
func isTokenHeaderClaimed(ds *policy.DownstreamContext, live *policy.Headers, headerName string) bool {
	if ds == nil || ds.Request == nil || ds.Request.Headers == nil {
		return false
	}
	original := ds.Request.Headers.Get(headerName)
	if len(original) == 0 {
		return false
	}
	return !slices.Equal(live.Get(headerName), original)
}

// preserveTokenHeader strips every modification that would delete or overwrite
// headerName, leaving the value its owner set in place.
//
// Both directions matter. The delegated JWT auth policy removes the inbound
// header unconditionally once it has consumed the token, and additionally
// rewrites it when forwardedTokenHeader names that same header. Each of those is
// correct in the header phase it was written for and wrong once a peer owns the
// header, so both are dropped.
func preserveTokenHeader(mods policy.UpstreamRequestHeaderModifications, headerName string) policy.UpstreamRequestHeaderModifications {
	canonical := http.CanonicalHeaderKey(headerName)
	sameHeader := func(name string) bool { return http.CanonicalHeaderKey(name) == canonical }

	mods.HeadersToRemove = slices.DeleteFunc(slices.Clone(mods.HeadersToRemove), sameHeader)
	if len(mods.HeadersToRemove) == 0 {
		mods.HeadersToRemove = nil
	}

	for name := range mods.HeadersToSet {
		if sameHeader(name) {
			delete(mods.HeadersToSet, name)
		}
	}
	return mods
}
