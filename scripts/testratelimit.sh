URL="https://takingoff.rickt.dev/cached?code=lax"
for i in $(seq 1 30); do
  code=$(curl -s -o /dev/null -w "%{http_code}" "$URL")
  echo "$i $code"
done
