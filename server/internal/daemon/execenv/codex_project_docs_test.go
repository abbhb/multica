package execenv

import (
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/pelletier/go-toml/v2"
)

func TestCodexProjectDocBudget(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name, config string
		want         int64
	}{
		{"default", "model = \"fixture\"\n[features]\nweb_search = true\n", 128 * 1024},
		{"small", "project_doc_max_bytes = 32768 # user limit\n", 128 * 1024},
		{"quoted", "'project_doc_max_bytes' = 32_768\r\n", 128 * 1024},
		{"larger", "project_doc_max_bytes = 262144\n", 256 * 1024},
		{"nested", "[profiles.work]\nproject_doc_max_bytes = 32768\n", 128 * 1024},
		{"multiline", "instructions = '''\nproject_doc_max_bytes = 1\n'''\n", 128 * 1024},
	} {
		t.Run(tc.name, func(t *testing.T) {
			config := filepath.Join(t.TempDir(), "config.toml")
			if err := os.WriteFile(config, []byte(tc.config), 0o600); err != nil {
				t.Fatal(err)
			}
			if err := ensureCodexProjectDocBudget(config); err != nil {
				t.Fatal(err)
			}
			data, err := os.ReadFile(config)
			if err != nil {
				t.Fatal(err)
			}
			var parsed map[string]any
			if err := toml.Unmarshal(data, &parsed); err != nil {
				t.Fatal(err)
			}
			if parsed["project_doc_max_bytes"] != tc.want {
				t.Fatalf("project doc budget = %v, want %d", parsed["project_doc_max_bytes"], tc.want)
			}
			if err := ensureCodexProjectDocBudget(config); err != nil {
				t.Fatal(err)
			}
			again, err := os.ReadFile(config)
			if err != nil || string(data) != string(again) {
				t.Fatalf("second prepare changed configuration: %v", err)
			}
			if tc.name == "larger" && string(data) != tc.config {
				t.Fatal("larger user budget was rewritten")
			}
			if tc.name == "nested" && !strings.Contains(string(data), "[profiles.work]\nproject_doc_max_bytes = 32768") {
				t.Fatal("profile configuration was changed")
			}
			if tc.name == "multiline" && !strings.Contains(string(data), tc.config) {
				t.Fatal("multiline string was changed")
			}
		})
	}
}

func TestCodexLongProjectInstructionsKeepFullRuntimeBrief(t *testing.T) {
	shared := t.TempDir()
	t.Setenv("CODEX_HOME", shared)
	sharedConfig := []byte("model = \"fixture\"\n")
	if err := os.WriteFile(filepath.Join(shared, "config.toml"), sharedConfig, 0o600); err != nil {
		t.Fatal(err)
	}
	root := t.TempDir()
	work := filepath.Join(root, "work")
	if err := os.Mkdir(work, 0o700); err != nil {
		t.Fatal(err)
	}
	project := "# Project instructions\n" + strings.Repeat("保留项目规则。\n", 1400)
	path := filepath.Join(work, "AGENTS.md")
	if err := os.WriteFile(path, []byte(project), 0o600); err != nil {
		t.Fatal(err)
	}
	brief, err := InjectRuntimeConfig(work, "codex", TaskContextForEnv{IssueID: "fixture-issue", AgentName: "Fixture"})
	if err != nil {
		t.Fatal(err)
	}
	home := filepath.Join(root, "codex-home")
	if err := prepareCodexHomeWithOpts(home, CodexHomeOptions{}, slog.Default()); err != nil {
		t.Fatal(err)
	}
	config, err := os.ReadFile(filepath.Join(home, "config.toml"))
	if err != nil {
		t.Fatal(err)
	}
	var parsed struct {
		Budget int `toml:"project_doc_max_bytes"`
	}
	if err := toml.Unmarshal(config, &parsed); err != nil {
		t.Fatal(err)
	}
	if parsed.Budget == 0 {
		parsed.Budget = 32 * 1024
	}
	instructions, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	loaded := string(instructions[:min(len(instructions), parsed.Budget)])
	if !strings.HasPrefix(loaded, project) || !strings.Contains(loaded, strings.TrimRight(brief, "\n")) {
		t.Fatalf("native document budget truncated the project or runtime brief: document=%d budget=%d", len(instructions), parsed.Budget)
	}
	unchanged, err := os.ReadFile(filepath.Join(shared, "config.toml"))
	if err != nil || string(unchanged) != string(sharedConfig) {
		t.Fatalf("shared Codex configuration was changed: %v", err)
	}
}
