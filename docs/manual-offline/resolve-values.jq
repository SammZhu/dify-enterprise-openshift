# Input: final offline vendor values. --slurpfile secrets /private/secrets.json
# Resolve explicit references only, without printing credentials or invoking oc.
. as $values
| [.. | strings | scan("@@secret:[a-z0-9-]+/[A-Za-z0-9_.-]+@@")] | unique as $refs
| reduce $refs[] as $ref ($values;
    ($ref | capture("@@secret:(?<name>[a-z0-9-]+)/(?<key>[A-Za-z0-9_.-]+)@@")) as $p
    | ([$secrets[0].items[] | select(.metadata.name == $p.name) | .data[$p.key]] | first) as $encoded
    | if $encoded == null or $encoded == "" then error("Missing required Secret key: " + $p.name + "/" + $p.key)
      else ($encoded | @base64d) as $value
      | walk(if type == "string" then split($ref) | join($value) else . end) end)
| if [.. | strings | select(contains("@@secret:"))] | length > 0
  then error("Unresolved Secret reference") else . end
