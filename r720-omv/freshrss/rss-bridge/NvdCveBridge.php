<?php

/**
 * New CVEs for one product, from the NVD CVE API 2.0.
 *
 * Why it is built this way:
 *  - keywordSearch finds a CVE as soon as NVD ingests the CVE record. Matching
 *    on CPE only works after NVD has analysed the CVE (days to weeks later)
 *    and is far slower (0.7 s vs 16 s for the same Traefik query).
 *  - Product names are inconsistent. One app appears under several names
 *    (Frigate is "blakeblackshear / frigate" from the CNA, "frigate / frigate"
 *    in NVD's CPE), and unrelated apps share a name ("frigate 3 professional",
 *    Komodo CMS vs moghtech's Komodo). So:
 *      - Product takes up to MAX_TERMS alternative names, one search each.
 *      - A hit is kept only if a name equals the vendor or product in the
 *        CNA's "affected" list or NVD's vulnerable CPEs (case, spaces and
 *        punctuation ignored). "vendor:product" pairs pin down clashes.
 *      - A CVE with no vendor/product data yet is kept but tagged, so a
 *        brand-new CVE is never lost to the filter.
 *  - RSS-Bridge's default HTTP timeout is 3 s with no retries, and a failed run
 *    is cached as an error item for the whole cache period. This bridge sets
 *    its own timeout, retries with backoff, and if NVD still fails it serves
 *    the last good result (up to STALE_MAX_AGE old) with a short cache period,
 *    so the next poll tries again. Longer outages surface as an error item.
 *
 * Optional NVD API key (https://nvd.nist.gov/developers/request-an-api-key)
 * raises the limit from 5 to 50 requests per 30 s:
 *   config.ini.php:  [NvdCveBridge] api_key = "..."
 *   or environment:  RSSBRIDGE_NvdCveBridge_api_key=...
 */
class NvdCveBridge extends BridgeAbstract
{
    const NAME = 'NVD CVE Watch';
    const URI = 'https://nvd.nist.gov';
    const DESCRIPTION = 'New CVEs for a specific product, from the NVD CVE API. '
        . 'Matches on the vendor/product data, not just the name, and rides out NVD outages.';
    const MAINTAINER = 'Dinth';
    // Per-feed jitter is added on top, see getCacheTimeout().
    const CACHE_TIMEOUT = 7200;

    const CONFIGURATION = [
        'api_key' => ['required' => false],
    ];

    const PARAMETERS = [[
        'q' => [
            'name' => 'Product',
            'required' => true,
            'exampleValue' => 'Traefik',
            'title' => 'Searched for in CVE descriptions; every word must appear. '
                . 'Up to 3 alternative names, comma-separated, e.g. "Home Assistant, HomeAssistant".',
        ],
        'names' => [
            'name' => 'Vendor/product names',
            'exampleValue' => 'moghtech:komodo',
            'title' => 'Comma-separated. A plain name must equal the vendor or the product '
                . '(case, spaces, punctuation and suffixes like Inc ignored). vendor:product '
                . 'requires both, e.g. "moghtech:komodo". A trailing * means "starts with", '
                . 'e.g. "microsoft:microsoft 365*"; a lone * matches anything. '
                . 'Defaults to the Product names. Each item lists the names vendors used.',
        ],
        'match' => [
            'name' => 'Match',
            'type' => 'list',
            'values' => [
                'Affected product (drop other products)' => 'product',
                'Everything (tag other products instead)' => 'description',
            ],
            'defaultValue' => 'product',
        ],
        'exact' => [
            'name' => 'Exact phrase',
            'type' => 'checkbox',
            'title' => 'Search for each Product name as one phrase instead of separate words',
        ],
        'severity' => [
            'name' => 'Minimum severity',
            'type' => 'list',
            'values' => [
                'Any' => '0',
                'Low' => '1',
                'Medium' => '2',
                'High' => '3',
                'Critical' => '4',
            ],
            'defaultValue' => '0',
        ],
        'scored' => [
            'name' => 'Hide unscored CVEs',
            'type' => 'checkbox',
            'title' => 'New CVEs often have no CVSS score yet and are kept whatever '
                . 'the minimum severity. Tick to drop them instead.',
        ],
        'days' => [
            'name' => 'Days to look back',
            'type' => 'number',
            'defaultValue' => 60,
            'title' => '1 to 120 (the NVD maximum)',
        ],
        'limit' => [
            'name' => 'Limit',
            'type' => 'number',
            'defaultValue' => 50,
            'title' => 'Maximum number of items, 1 to 500. Keep it above the largest '
                . 'batch released at once (Microsoft 365: ~175 on one Patch Tuesday).',
        ],
    ]];

    private const API_URL = 'https://services.nvd.nist.gov/rest/json/cves/2.0';
    // NVD allows 2000, but a 2000-CVE page of "linux kernel" is 10 MB of JSON
    // and several times that once decoded. 500 covers most products in one
    // request, and a broad keyword still gets its newest 500.
    private const PAGE_SIZE = 500;
    private const MAX_TERMS = 3;
    private const MAX_ATTEMPTS = 4;
    private const REQUEST_TIMEOUT = 20;
    // RSS-Bridge's nginx gives php-fpm 45 s (fastcgi_read_timeout).
    private const TIME_BUDGET = 35;
    private const STALE_MAX_AGE = 172800;
    private const STALE_RETRY_TTL = 900;
    private const SNAPSHOT_TTL = 2592000;

    private const SEVERITY_RANK = ['NONE' => 0, 'LOW' => 1, 'MEDIUM' => 2, 'HIGH' => 3, 'CRITICAL' => 4];
    // CVSS 3.1 first: it is what NVD severities and most tooling use.
    private const CVSS_KEYS = [
        'cvssMetricV31' => 'CVSS 3.1',
        'cvssMetricV30' => 'CVSS 3.0',
        'cvssMetricV40' => 'CVSS 4.0',
        'cvssMetricV2' => 'CVSS 2.0',
    ];
    private const PLACEHOLDER_NAMES = ['', 'n/a', 'na', '-', '*', 'unknown', 'unspecified'];
    // Trailing words dropped from vendor names, so "Ubiquiti Inc", "Ubiquiti
    // Networks", NVD's "ubiquiti_networks" and "TP-Link Systems Inc." match
    // "ubiquiti" and "tp-link".
    private const VENDOR_SUFFIXES = [
        'inc', 'incorporated', 'ltd', 'limited', 'llc', 'corp', 'corporation', 'co', 'company',
        'gmbh', 'ag', 'sa', 'plc', 'bv', 'pty', 'srl', 'sas',
        'systems', 'networks', 'software', 'technologies', 'technology', 'group',
    ];

    // Outcomes of the vendor/product check.
    private const PRODUCT_MATCH = 'match';
    private const PRODUCT_OTHER = 'other';
    private const PRODUCT_UNKNOWN = 'unknown';

    private bool $servedStale = false;
    private int $requestCount = 0;

    public function collectData()
    {
        $snapshotKey = 'snapshot_' . md5(Json::encode($this->feedParams(), false));

        try {
            $cves = $this->fetchCves();
        } catch (ClientException $e) {
            // Bad input or NVD rejected the query: old data would only hide that.
            throw $e;
        } catch (\Throwable $e) {
            $snapshot = $this->loadCacheValue($snapshotKey);
            $age = is_array($snapshot) ? time() - (int) ($snapshot['time'] ?? 0) : null;
            if ($age === null || $age > self::STALE_MAX_AGE) {
                throw $e;
            }
            $this->logger->warning(sprintf(
                'NvdCveBridge: NVD fetch for "%s" failed, serving the result from %d min ago: %s',
                $this->getInput('q'),
                intdiv($age, 60),
                create_sane_exception_message($e)
            ));
            $this->items = $snapshot['items'];
            $this->servedStale = true;
            return;
        }

        $rules = self::parseNameRules((string) $this->getInput('names'), $this->searchTerms());
        $strict = $this->getInput('match') !== 'description';
        $minRank = (int) $this->getInput('severity');
        $hideUnscored = (bool) $this->getInput('scored');

        $items = [];
        foreach ($cves as $cve) {
            $productMatch = self::productMatch($cve, $rules);
            if ($strict && $productMatch === self::PRODUCT_OTHER) {
                continue;
            }
            $score = self::pickScore($cve['metrics'] ?? []);
            if ($score === null) {
                if ($hideUnscored) {
                    continue;
                }
            } elseif ((self::SEVERITY_RANK[$score['severity']] ?? 0) < $minRank) {
                continue;
            }
            $items[] = $this->buildItem($cve, $score, $productMatch);
        }

        usort($items, fn ($a, $b) => $b['timestamp'] <=> $a['timestamp']);
        $this->items = array_slice($items, 0, $this->clampedInput('limit', 50, 1, 500));

        $this->saveCacheValue($snapshotKey, ['time' => time(), 'items' => $this->items], self::SNAPSHOT_TTL);
    }

    public function getName()
    {
        $terms = $this->searchTerms();
        if (!$terms) {
            return self::NAME;
        }
        $name = 'CVEs: ' . implode(', ', $terms);
        $rank = (int) $this->getInput('severity');
        if ($rank > 0) {
            $name .= ' (' . ucfirst(strtolower((string) array_search($rank, self::SEVERITY_RANK, true))) . '+)';
        }
        return $name;
    }

    public function getURI()
    {
        return 'https://nvd.nist.gov/vuln/search';
    }

    public function getCacheTimeout()
    {
        if ($this->servedStale) {
            return self::STALE_RETRY_TTL;
        }
        // Up to +25 % per feed, so feeds created together drift apart instead
        // of all hitting NVD's rate limit in the same poll.
        return self::CACHE_TIMEOUT
            + crc32(Json::encode($this->feedParams(), false)) % intdiv(self::CACHE_TIMEOUT, 4);
    }

    private function feedParams(): array
    {
        $params = [];
        foreach (array_keys(self::PARAMETERS[0]) as $name) {
            $params[$name] = $this->getInput($name);
        }
        return $params;
    }

    private function clampedInput(string $name, int $default, int $min, int $max): int
    {
        $value = $this->getInput($name);
        if (!is_numeric($value)) {
            return $default;
        }
        return max($min, min($max, (int) $value));
    }

    private function searchTerms(): array
    {
        $terms = [];
        foreach (explode(',', (string) $this->getInput('q')) as $term) {
            $term = trim($term);
            if ($term !== '') {
                $terms[strtolower($term)] = $term;
            }
        }
        return array_values($terms);
    }

    private function fetchCves(): array
    {
        $terms = $this->searchTerms();
        if (!$terms) {
            throw new ClientException('Product is required');
        }
        if (count($terms) > self::MAX_TERMS) {
            throw new ClientException(sprintf('Product takes at most %d comma-separated names', self::MAX_TERMS));
        }

        // Whole UTC days keep the URL stable for a day, which keeps getContents()'
        // response cache to one entry per search per day. NVD caps the range at
        // 120 days; this spans just under $days.
        $days = $this->clampedInput('days', 60, 1, 120);
        $end = (new \DateTimeImmutable('now', new \DateTimeZone('UTC')))->setTime(23, 59, 59, 999000);
        $start = $end->setTime(0, 0)->modify(sprintf('-%d days', $days - 1));
        $deadline = microtime(true) + self::TIME_BUDGET;

        $cves = [];
        foreach ($terms as $term) {
            $query = [
                'keywordSearch' => $term,
                'pubStartDate' => $start->format('Y-m-d\TH:i:s.v\Z'),
                'pubEndDate' => $end->format('Y-m-d\TH:i:s.v\Z'),
                'noRejected' => '',
                'resultsPerPage' => self::PAGE_SIZE,
                'startIndex' => 0,
            ];
            if ($this->getInput('exact')) {
                $query['keywordExactMatch'] = '';
            }

            $page = $this->request($query, $deadline);
            $total = (int) $page['totalResults'];
            if ($total > count($page['vulnerabilities'])) {
                // NVD returns the oldest first, so the first page would miss
                // the newest CVEs. Replace it with the last page.
                $query['startIndex'] = max(0, $total - self::PAGE_SIZE);
                $page = $this->request($query, $deadline);
            }

            foreach ($page['vulnerabilities'] as $vulnerability) {
                $id = $vulnerability['cve']['id'] ?? null;
                if (is_string($id)) {
                    $cves[$id] = $vulnerability['cve'];
                }
            }
        }
        return array_values($cves);
    }

    private function request(array $query, float $deadline): array
    {
        $url = self::API_URL . '?' . http_build_query($query, '', '&', PHP_QUERY_RFC3986);
        $headers = ['Accept: application/json'];
        $apiKey = trim((string) $this->getOption('api_key'));
        if ($apiKey !== '') {
            $headers[] = 'apiKey: ' . $apiKey;
        }

        // NVD's recommended gap between requests (6 s without a key).
        if ($this->requestCount++ > 0) {
            usleep($apiKey !== '' ? 1000000 : 6000000);
        }

        $error = '';
        for ($attempt = 1; ; $attempt++) {
            $remaining = $deadline - microtime(true);
            // A timeout of 0 means "none" to cURL, which nginx would cut off.
            if ($remaining < 3) {
                throw new \Exception(rtrim('NVD API: out of time. ' . $error));
            }
            $timeout = (int) min(self::REQUEST_TIMEOUT, floor($remaining));
            try {
                $body = getContents($url, $headers, [
                    CURLOPT_TIMEOUT => $timeout,
                    CURLOPT_CONNECTTIMEOUT => min(10, $timeout),
                ]);
                $data = json_decode($body, true);
                if (
                    is_array($data)
                    && isset($data['totalResults'])
                    && is_array($data['vulnerabilities'] ?? null)
                ) {
                    return $data;
                }
                // An HTML error page or truncated JSON must never pass as "no CVEs".
                $error = 'unexpected response body: ' . mb_substr(trim(strip_tags($body)), 0, 120);
                $wait = 3 * $attempt;
            } catch (HttpException $e) {
                $code = $e->getCode();
                // NVD explains rejected parameters in a "message" header.
                $message = (string) $e->response->getHeader('message');
                if (in_array($code, [400, 404], true) && $message !== '') {
                    throw new ClientException('NVD rejected the query: ' . $message);
                }
                // 0 = network/cURL error. NVD answers 403 when rate limited.
                if (!in_array($code, [0, 403, 408, 429], true) && $code < 500) {
                    throw $e;
                }
                $error = create_sane_exception_message($e) . ($message !== '' ? " ($message)" : '');
                $retryAfter = (int) $e->response->getHeader('retry-after');
                $wait = in_array($code, [403, 429], true) ? max(6 * $attempt, $retryAfter) : 3 * $attempt;
            }

            if ($attempt >= self::MAX_ATTEMPTS || microtime(true) + $wait + 5 > $deadline) {
                throw new \Exception(sprintf('NVD API failed after %d attempt(s): %s', $attempt, $error));
            }
            $this->logger->info(sprintf('NvdCveBridge: attempt %d failed, retrying in %d s: %s', $attempt, $wait, $error));
            usleep($wait * 1000000);
        }
    }

    private function buildItem(array $cve, ?array $score, string $productMatch): array
    {
        $id = $cve['id'];
        $description = self::englishDescription($cve);
        $exploited = self::exploitedSignal($cve);

        $title = $id;
        if ($exploited) {
            $title .= ' [EXPLOITED]';
        }
        $title .= $score ? sprintf(' [%s %.1f]', $score['severity'], $score['score']) : ' [unscored]';
        $title .= ' ' . preg_replace('/\s+/', ' ', $description);

        $categories = [$score['severity'] ?? 'UNSCORED'];
        if ($exploited) {
            $categories[] = 'Exploited';
        }
        if (!empty($cve['cisaExploitAdd'])) {
            $categories[] = 'CISA KEV';
        }
        if ($productMatch === self::PRODUCT_UNKNOWN) {
            $categories[] = 'Unverified product';
        } elseif ($productMatch === self::PRODUCT_OTHER) {
            $categories[] = 'Other product';
        }
        foreach (self::cwes($cve) as $cwe) {
            $categories[] = $cwe;
        }
        if (!empty($cve['vulnStatus'])) {
            $categories[] = $cve['vulnStatus'];
        }

        return [
            'uid' => $id,
            'uri' => 'https://nvd.nist.gov/vuln/detail/' . rawurlencode($id),
            'title' => $title,
            'timestamp' => self::utcTimestamp($cve['published'] ?? ''),
            'content' => $this->buildContent($cve, $description, $productMatch),
            'categories' => array_values(array_unique($categories)),
        ];
    }

    private function buildContent(array $cve, string $description, string $productMatch): string
    {
        $id = $cve['id'];
        $html = '<p>' . e($description) . '</p>';

        if ($productMatch === self::PRODUCT_UNKNOWN) {
            $html .= '<p><em>No vendor/product data yet, so this matched on the description only. '
                . 'Check it is your product.</em></p>';
        } elseif ($productMatch === self::PRODUCT_OTHER) {
            $html .= '<p><em>None of the affected vendor/product names below matches, '
                . 'so this is probably a different product.</em></p>';
        }

        if (!empty($cve['cisaExploitAdd'])) {
            $html .= sprintf(
                '<p><strong>In CISA KEV</strong> since %s (federal deadline %s). %s</p>',
                e((string) $cve['cisaExploitAdd']),
                e((string) ($cve['cisaActionDue'] ?? '?')),
                e((string) ($cve['cisaRequiredAction'] ?? ''))
            );
        }

        $rows = [];
        foreach (self::CVSS_KEYS as $key => $label) {
            foreach ($cve['metrics'][$key] ?? [] as $metric) {
                $s = self::metricScore($metric);
                if ($s === null) {
                    continue;
                }
                $rows[] = [$label, sprintf(
                    '%.1f %s (%s) <code>%s</code>',
                    $s['score'],
                    e($s['severity']),
                    ($metric['type'] ?? '') === 'Primary' ? 'NVD' : 'CNA',
                    e((string) ($metric['cvssData']['vectorString'] ?? ''))
                )];
            }
        }
        $ssvc = self::ssvc($cve);
        if ($ssvc) {
            $parts = [];
            foreach ($ssvc as $name => $value) {
                $parts[] = e($name) . ': ' . e($value);
            }
            $rows[] = ['CISA SSVC', implode(' · ', $parts)];
        }
        $cweLinks = [];
        foreach (self::cwes($cve) as $cwe) {
            $cweLinks[] = sprintf(
                '<a href="https://cwe.mitre.org/data/definitions/%d.html">%s</a>',
                (int) substr($cwe, 4),
                e($cwe)
            );
        }
        if ($cweLinks) {
            $rows[] = ['Weakness', implode(', ', $cweLinks)];
        }
        $rows[] = ['NVD status', e((string) ($cve['vulnStatus'] ?? '?'))];
        $rows[] = ['Published', e(self::formatDate($cve['published'] ?? ''))
            . ' (modified ' . e(self::formatDate($cve['lastModified'] ?? '')) . ')'];
        foreach ($cve['cveTags'] ?? [] as $tagSet) {
            foreach ($tagSet['tags'] ?? [] as $tag) {
                $rows[] = ['Tag', e((string) $tag)];
            }
        }

        $html .= '<table>';
        foreach ($rows as [$label, $value]) {
            $html .= '<tr><th align="left">' . e($label) . '</th><td>' . $value . '</td></tr>';
        }
        $html .= '</table>';

        $affected = self::affectedLines($cve);
        if ($affected) {
            $html .= '<p><strong>Affected</strong></p><ul><li>' . implode('</li><li>', $affected) . '</li></ul>';
        }

        $references = [];
        foreach ($cve['references'] ?? [] as $reference) {
            $url = (string) ($reference['url'] ?? '');
            if (!preg_match('#^https?://#i', $url)) {
                continue;
            }
            $line = '<a href="' . e($url) . '">' . e($url) . '</a>';
            if (!empty($reference['tags'])) {
                $line .= ' (' . e(implode(', ', $reference['tags'])) . ')';
            }
            $references[$url] = $line;
        }
        if ($references) {
            $html .= '<p><strong>References</strong></p><ul><li>'
                . implode('</li><li>', array_slice($references, 0, 20)) . '</li></ul>';
        }

        $html .= sprintf(
            '<p><a href="https://nvd.nist.gov/vuln/detail/%1$s">NVD</a> · '
                . '<a href="https://www.cve.org/CVERecord?id=%1$s">CVE.org</a></p>',
            e(rawurlencode($id))
        );
        return $html;
    }

    /**
     * Parse the "names" input into rules:
     *   ['any' => [vendor pattern, product pattern]]  plain name: vendor or product
     *   ['vendor' => pattern|null, 'product' => pattern|null]  vendor:product, null = *
     * A pattern is ['keys' => string[], 'prefix' => bool] (trailing * = prefix).
     */
    private static function parseNameRules(string $names, array $searchTerms): array
    {
        $entries = trim($names) === '' ? $searchTerms : explode(',', $names);
        $rules = [];
        foreach ($entries as $entry) {
            $entry = trim($entry);
            if (strpos($entry, ':') !== false) {
                [$vendor, $product] = explode(':', $entry, 2);
                $rule = ['vendor' => self::pattern($vendor, true), 'product' => self::pattern($product, false)];
                if ($rule['vendor'] !== null || $rule['product'] !== null) {
                    $rules[] = $rule;
                }
            } else {
                $vendor = self::pattern($entry, true);
                $product = self::pattern($entry, false);
                if ($vendor !== null && $product !== null) {
                    $rules[] = ['any' => [$vendor, $product]];
                }
            }
        }
        return $rules;
    }

    private static function pattern(string $text, bool $isVendor): ?array
    {
        $text = trim($text);
        $prefix = substr($text, -1) === '*';
        $text = rtrim($text, '*');
        $keys = $isVendor ? self::vendorKeys($text) : array_filter([self::nameKey($text)]);
        return $keys ? ['keys' => array_values($keys), 'prefix' => $prefix] : null;
    }

    private static function patternMatches(array $pattern, array $candidates): bool
    {
        foreach ($pattern['keys'] as $key) {
            foreach ($candidates as $candidate) {
                if ($candidate === $key || ($pattern['prefix'] && strpos($candidate, $key) === 0)) {
                    return true;
                }
            }
        }
        return false;
    }

    private static function nameKey(string $name): ?string
    {
        $key = preg_replace('/[^a-z0-9]+/', '', strtolower($name));
        return $key === '' ? null : $key;
    }

    /**
     * A vendor name as written and without trailing company suffixes.
     */
    private static function vendorKeys(string $vendor): array
    {
        $tokens = preg_split('/[^a-z0-9]+/', strtolower($vendor), -1, PREG_SPLIT_NO_EMPTY) ?: [];
        if (!$tokens) {
            return [];
        }
        $keys = [implode('', $tokens)];
        while (count($tokens) > 1 && in_array(end($tokens), self::VENDOR_SUFFIXES, true)) {
            array_pop($tokens);
        }
        $keys[] = implode('', $tokens);
        return array_values(array_unique($keys));
    }

    /**
     * Whether the CVE's vendor/product data (from the CNA and from NVD's CPEs)
     * names the product: PRODUCT_MATCH, PRODUCT_OTHER, or PRODUCT_UNKNOWN when
     * there is no such data yet.
     */
    private static function productMatch(array $cve, array $rules): string
    {
        $entries = self::productEntries($cve);
        if (!$entries) {
            return self::PRODUCT_UNKNOWN;
        }
        foreach ($entries as $entry) {
            foreach ($rules as $rule) {
                if (self::entryMatches($entry, $rule)) {
                    return self::PRODUCT_MATCH;
                }
            }
        }
        return self::PRODUCT_OTHER;
    }

    /**
     * @return array[] ['vendor' => vendorKeys(), 'product' => key|null, 'extra' => token lists]
     */
    private static function productEntries(array $cve): array
    {
        $entries = [];
        foreach ($cve['affected'] ?? [] as $source) {
            foreach ($source['affectedData'] ?? [] as $affected) {
                $extra = [];
                // Package and repo identifiers, e.g. pkg:golang/github.com/traefik/traefik/v3
                // or https://git.kernel.org/.../linux.git, matched as consecutive words.
                foreach (['packageName', 'packageURL', 'repo'] as $field) {
                    $value = self::realName($affected[$field] ?? '');
                    if ($value !== '') {
                        $extra[] = preg_split('/[^a-z0-9]+/', strtolower($value), -1, PREG_SPLIT_NO_EMPTY) ?: [];
                    }
                }
                $vendor = self::vendorKeys(self::realName($affected['vendor'] ?? ''));
                $product = self::nameKey(self::realName($affected['product'] ?? ''));
                if ($vendor || $product !== null || $extra) {
                    $entries[] = ['vendor' => $vendor, 'product' => $product, 'extra' => $extra];
                }
            }
        }
        foreach (self::vulnerableCpes($cve) as $match) {
            // cpe:2.3:part:vendor:product:version:...
            $parts = explode(':', $match['criteria'] ?? '');
            $entries[] = [
                'vendor' => self::vendorKeys($parts[3] ?? ''),
                'product' => self::nameKey($parts[4] ?? ''),
                'extra' => [],
            ];
        }
        return $entries;
    }

    private static function realName($value): string
    {
        $value = is_string($value) ? trim($value) : '';
        return in_array(strtolower($value), self::PLACEHOLDER_NAMES, true) ? '' : $value;
    }

    private static function entryMatches(array $entry, array $rule): bool
    {
        $products = $entry['product'] === null ? [] : [$entry['product']];

        if (isset($rule['any'])) {
            [$vendorPattern, $productPattern] = $rule['any'];
            if (self::patternMatches($vendorPattern, $entry['vendor']) || self::patternMatches($productPattern, $products)) {
                return true;
            }
            // "home assistant core" = vendor "home-assistant" + product "core".
            if ($entry['product'] !== null) {
                $combined = array_map(fn ($vendor) => $vendor . $entry['product'], $entry['vendor']);
                if (self::patternMatches($productPattern, $combined)) {
                    return true;
                }
            }
            foreach ($entry['extra'] as $tokens) {
                foreach ($productPattern['keys'] as $key) {
                    if (self::tokensContain($tokens, $key)) {
                        return true;
                    }
                }
            }
            return false;
        }

        return ($rule['vendor'] === null || self::patternMatches($rule['vendor'], $entry['vendor']))
            && ($rule['product'] === null || self::patternMatches($rule['product'], $products));
    }

    /**
     * Whether consecutive $tokens join to exactly $target, so "linux kernel"
     * is found in ".../linux/kernel/git/..." while "go" is not found in "golang".
     */
    private static function tokensContain(array $tokens, string $target): bool
    {
        $count = count($tokens);
        for ($i = 0; $i < $count; $i++) {
            $joined = '';
            for ($j = $i; $j < $count && strlen($joined) < strlen($target); $j++) {
                $joined .= $tokens[$j];
                if ($joined === $target) {
                    return true;
                }
            }
        }
        return false;
    }

    private static function vulnerableCpes(array $cve): array
    {
        $matches = [];
        foreach ($cve['configurations'] ?? [] as $configuration) {
            foreach ($configuration['nodes'] ?? [] as $node) {
                foreach ($node['cpeMatch'] ?? [] as $match) {
                    // vulnerable=false entries are platforms ("running on Linux").
                    if (!empty($match['vulnerable'])) {
                        $matches[] = $match;
                    }
                }
            }
        }
        return $matches;
    }

    private static function pickScore(array $metrics): ?array
    {
        foreach (array_keys(self::CVSS_KEYS) as $key) {
            $list = $metrics[$key] ?? [];
            // NVD's own (Primary) assessment ahead of the CNA's (Secondary).
            usort($list, fn ($a, $b) => (($b['type'] ?? '') === 'Primary') <=> (($a['type'] ?? '') === 'Primary'));
            foreach ($list as $metric) {
                $score = self::metricScore($metric);
                if ($score !== null) {
                    return $score;
                }
            }
        }
        return null;
    }

    private static function metricScore(array $metric): ?array
    {
        $score = $metric['cvssData']['baseScore'] ?? null;
        // CVSS 2 keeps the severity outside cvssData.
        $severity = strtoupper((string) ($metric['cvssData']['baseSeverity'] ?? $metric['baseSeverity'] ?? ''));
        if (!is_numeric($score) || !isset(self::SEVERITY_RANK[$severity])) {
            return null;
        }
        return ['score' => (float) $score, 'severity' => $severity];
    }

    private static function exploitedSignal(array $cve): bool
    {
        return !empty($cve['cisaExploitAdd']) || (self::ssvc($cve)['Exploitation'] ?? '') === 'active';
    }

    private static function ssvc(array $cve): array
    {
        $labels = ['exploitation' => 'Exploitation', 'automatable' => 'Automatable', 'technicalImpact' => 'Technical impact'];
        $result = [];
        foreach ($cve['metrics'] ?? [] as $key => $list) {
            if (strpos($key, 'ssvc') !== 0) {
                continue;
            }
            foreach ($list as $metric) {
                foreach ($metric['ssvcData']['options'] ?? [] as $option) {
                    foreach ($option as $name => $value) {
                        if (isset($labels[$name]) && is_string($value)) {
                            $result[$labels[$name]] = strtolower($value);
                        }
                    }
                }
            }
        }
        return $result;
    }

    private static function cwes(array $cve): array
    {
        $cwes = [];
        foreach ($cve['weaknesses'] ?? [] as $weakness) {
            foreach ($weakness['description'] ?? [] as $description) {
                $value = (string) ($description['value'] ?? '');
                if (preg_match('/^CWE-\d+$/', $value)) {
                    $cwes[$value] = true;
                }
            }
        }
        return array_keys($cwes);
    }

    /**
     * Both the CNA's and NVD's names are listed, so it is visible which names
     * to put in the "names" parameter.
     */
    private static function affectedLines(array $cve): array
    {
        $lines = [];
        foreach ($cve['affected'] ?? [] as $source) {
            foreach ($source['affectedData'] ?? [] as $entry) {
                $name = trim(($entry['vendor'] ?? '') . ' / ' . ($entry['product'] ?? ''), ' /');
                if (!empty($entry['packageURL'])) {
                    $name .= ' (' . $entry['packageURL'] . ')';
                }
                $line = e($name) . ': ' . e(self::versionRanges($entry['versions'] ?? []));
                $lines[$line] = true;
            }
        }
        foreach (array_slice(self::vulnerableCpes($cve), 0, 15) as $match) {
            $parts = explode(':', $match['criteria'] ?? '');
            $range = [];
            if (isset($match['versionStartIncluding'])) {
                $range[] = '>= ' . $match['versionStartIncluding'];
            }
            if (isset($match['versionStartExcluding'])) {
                $range[] = '> ' . $match['versionStartExcluding'];
            }
            if (isset($match['versionEndExcluding'])) {
                $range[] = '< ' . $match['versionEndExcluding'];
            }
            if (isset($match['versionEndIncluding'])) {
                $range[] = '<= ' . $match['versionEndIncluding'];
            }
            $version = $parts[5] ?? '*';
            if (!$range) {
                $range[] = in_array($version, ['*', '-'], true) ? 'all versions' : $version;
            }
            $line = 'NVD CPE ' . e(stripslashes(($parts[3] ?? '?') . ' / ' . ($parts[4] ?? '?')))
                . ': ' . e(implode(', ', $range));
            $lines[$line] = true;
        }
        return array_keys($lines);
    }

    private static function versionRanges(array $versions): string
    {
        $affected = [];
        $fixed = [];
        foreach ($versions as $v) {
            $version = (string) ($v['version'] ?? '');
            $from = in_array($version, ['', '0', '*'], true) ? '' : '>= ' . $version;
            $status = $v['status'] ?? '';
            if ($status === 'affected') {
                if (isset($v['lessThan'])) {
                    $to = $v['lessThan'] === '*' ? '' : '< ' . $v['lessThan'];
                } elseif (isset($v['lessThanOrEqual'])) {
                    $to = $v['lessThanOrEqual'] === '*' ? '' : '<= ' . $v['lessThanOrEqual'];
                } else {
                    $affected[] = $version;
                    continue;
                }
                $affected[] = trim($from . ($from && $to ? ', ' : '') . $to) ?: 'all versions';
            } elseif ($status === 'unaffected' && $version !== '' && !isset($v['lessThan']) && !isset($v['lessThanOrEqual'])) {
                $fixed[] = $version;
            }
        }
        $text = $affected ? implode('; ', array_unique($affected)) : 'see advisory';
        if ($fixed) {
            $text .= ' (fixed in ' . implode(', ', array_unique($fixed)) . ')';
        }
        return $text;
    }

    private static function englishDescription(array $cve): string
    {
        $descriptions = $cve['descriptions'] ?? [];
        foreach ($descriptions as $description) {
            if (($description['lang'] ?? '') === 'en') {
                return trim((string) $description['value']);
            }
        }
        return trim((string) ($descriptions[0]['value'] ?? ''));
    }

    private static function utcTimestamp(string $nvdDate): int
    {
        // NVD timestamps are UTC but carry no offset.
        try {
            return (new \DateTimeImmutable($nvdDate, new \DateTimeZone('UTC')))->getTimestamp();
        } catch (\Exception $e) {
            return time();
        }
    }

    private static function formatDate(string $nvdDate): string
    {
        return $nvdDate === '' ? '?' : gmdate('Y-m-d H:i', self::utcTimestamp($nvdDate)) . ' UTC';
    }
}
