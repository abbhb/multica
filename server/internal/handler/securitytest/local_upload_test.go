package securitytest

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"github.com/go-chi/chi/v5"
	"github.com/multica-ai/multica/server/internal/auth"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/multica-ai/multica/server/internal/handler"
	"github.com/multica-ai/multica/server/internal/middleware"
	"github.com/multica-ai/multica/server/internal/storage"
)

func TestLocalUploadAuthorization(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("LOCAL_UPLOAD_DIR", dir)
	for _, owner := range []string{"me", "victim"} {
		p := filepath.Join(dir, "users", owner, "photo.png")
		if err := os.MkdirAll(filepath.Dir(p), 0755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(owner), 0600); err != nil {
			t.Fatal(err)
		}
	}
	h := &handler.Handler{Storage: storage.NewLocalStorageFromEnv()}
	for _, tc := range []struct {
		path, user, actor string
		want              int
	}{
		{"/uploads/users/me/photo.png", "me", "", http.StatusOK},
		{"/uploads/users/victim/photo.png", "me", "", http.StatusNotFound},
		{"/uploads/users/me/../../users/victim/photo.png", "me", "", http.StatusNotFound},
		{"/uploads/users/me/photo.png", "me", "task_token", http.StatusNotFound},
		{"/uploads/users/me/", "me", "", http.StatusNotFound},
	} {
		req := httptest.NewRequest(http.MethodGet, tc.path, nil)
		req.Header.Set("X-User-ID", tc.user)
		req.Header.Set("X-Actor-Source", tc.actor)
		rec := httptest.NewRecorder()
		h.ServeLocalUpload(rec, req)
		if rec.Code != tc.want {
			t.Errorf("%s actor=%s got %d want %d", tc.path, tc.actor, rec.Code, tc.want)
		}
		if tc.want == http.StatusOK && rec.Header().Get("Cache-Control") != "private, no-store" {
			t.Error("missing private cache control")
		}
	}
	// The production route applies Auth before the handler, so client-supplied
	// identity headers alone must not grant access.
	req := httptest.NewRequest(http.MethodGet, "/uploads/users/me/photo.png", nil)
	req.Header.Set("X-User-ID", "me")
	rec := httptest.NewRecorder()
	middleware.Auth(nil, nil, nil, nil)(http.HandlerFunc(h.ServeLocalUpload)).ServeHTTP(rec, req)
	if rec.Code != http.StatusUnauthorized {
		t.Errorf("anonymous request got %d", rec.Code)
	}
}

func TestSignedAvatarRejectsNonCanonicalKey(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("LOCAL_UPLOAD_DIR", dir)
	p := filepath.Join(dir, "users", "victim", "secret.png")
	if err := os.MkdirAll(filepath.Dir(p), 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, []byte("private"), 0600); err != nil {
		t.Fatal(err)
	}
	h := &handler.Handler{Storage: storage.NewLocalStorageFromEnv()}
	key := "users/me/../../users/victim/secret.png"
	secret := sha256.Sum256(append([]byte("avatar-url:"), auth.JWTSecret()...))
	mac := hmac.New(sha256.New, secret[:])
	mac.Write([]byte(key))
	sig := base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
	req := httptest.NewRequest(http.MethodGet, "/api/avatars/test", nil)
	ctx := chi.NewRouteContext()
	ctx.URLParams.Add("sig", sig)
	ctx.URLParams.Add("*", key)
	req = req.WithContext(context.WithValue(req.Context(), chi.RouteCtxKey, ctx))
	rec := httptest.NewRecorder()
	h.ServeAvatar(rec, req)
	if rec.Code != http.StatusNotFound {
		t.Errorf("noncanonical signed avatar got %d", rec.Code)
	}
}
