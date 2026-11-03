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
	"github.com/multica-ai/multica/server/internal/storage"
)

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
