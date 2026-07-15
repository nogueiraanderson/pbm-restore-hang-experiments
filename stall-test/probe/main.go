// FileStat timing probe for the storage-deadline A/B test.
// Usage: probe <endpoint-url>
// Calls s3.FileStat once against the endpoint and prints how long the call
// took and what it returned. Against a black-hole endpoint (accepts TCP,
// never responds) the pristine build blocks forever; a build with response
// deadlines returns an error within the configured bounds.
package main

import (
	"fmt"
	"os"
	"time"

	"github.com/percona/percona-backup-mongodb/pbm/storage/s3"
)

func main() {
	if len(os.Args) != 2 {
		fmt.Fprintln(os.Stderr, "usage: probe <endpoint-url>")
		os.Exit(2)
	}
	fps := true
	cfg := &s3.Config{
		Region:         "us-east-1",
		EndpointURL:    os.Args[1],
		Bucket:         "probe",
		ForcePathStyle: &fps,
	}
	cfg.Credentials.AccessKeyID = envOr("PROBE_ACCESS_KEY", "probe")
	cfg.Credentials.SecretAccessKey = envOr("PROBE_SECRET_KEY", "probe")

	stg, err := s3.New(cfg, "probe-node", nil)
	if err != nil {
		fmt.Printf("RESULT new err=%v\n", err)
		os.Exit(2)
	}

	start := time.Now()
	_, err = stg.FileStat("probe-object")
	fmt.Printf("RESULT elapsed=%s err=%v\n", time.Since(start).Round(time.Millisecond), err)
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
