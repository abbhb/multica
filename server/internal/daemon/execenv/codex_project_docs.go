package execenv

import (
	"fmt"
	"os"
	"strconv"

	"github.com/pelletier/go-toml/v2"
	"github.com/pelletier/go-toml/v2/unstable"
)

const codexProjectDocMinBytes int64 = 128 * 1024

// The runtime brief follows the repository's own AGENTS.md content. Codex's
// 32 KiB default can truncate the entire brief in repositories with long rules.
// Enlarge only the task-local config, preserving larger user budgets and all
// unrelated TOML bytes, including comments and quoted keys.
func ensureCodexProjectDocBudget(configPath string) error {
	data, err := os.ReadFile(configPath)
	if err != nil && !os.IsNotExist(err) {
		return fmt.Errorf("read Codex project document config: %w", err)
	}
	var config map[string]any
	if err := toml.Unmarshal(data, &config); err != nil {
		return fmt.Errorf("parse Codex project document config: %w", err)
	}
	if value, exists := config["project_doc_max_bytes"]; exists {
		budget, ok := value.(int64)
		if !ok || budget < 0 {
			return fmt.Errorf("Codex project_doc_max_bytes must be a nonnegative integer")
		}
		if budget >= codexProjectDocMinBytes {
			return nil
		}
		var parser unstable.Parser
		parser.Reset(data)
		for parser.NextExpression() {
			expr := parser.Expression()
			if expr.Kind == unstable.Table || expr.Kind == unstable.ArrayTable {
				break
			}
			keys := tomlNodeKeys(expr)
			if expr.Kind != unstable.KeyValue || len(keys) != 1 || keys[0] != "project_doc_max_bytes" {
				continue
			}
			value := expr.Value().Raw
			start, end := int(value.Offset), int(value.Offset+value.Length)
			updated := string(data[:start]) + strconv.FormatInt(codexProjectDocMinBytes, 10) + string(data[end:])
			return os.WriteFile(configPath, []byte(updated), 0o644)
		}
		return fmt.Errorf("Codex project_doc_max_bytes root value was not found")
	}
	updated := "# Keep repository instructions and the Multica runtime brief in context.\n" +
		"project_doc_max_bytes = " + strconv.FormatInt(codexProjectDocMinBytes, 10) + "\n" + string(data)
	return os.WriteFile(configPath, []byte(updated), 0o644)
}
