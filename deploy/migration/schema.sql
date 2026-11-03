-- Capture the exact running target schema before preparing an import.
SELECT json_agg(row_to_json(c) ORDER BY table_name, ordinal_position)
FROM (
  SELECT table_name, column_name, data_type, udt_name, is_nullable,
         column_default, ordinal_position
  FROM information_schema.columns
  WHERE table_schema = 'public'
) c;
