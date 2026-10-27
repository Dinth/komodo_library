<?php

// Guzzle middleware for laravel/ai's Ollama client, attached by the start-time
// patch in compose.yaml (see the entrypoint there).
//
// laravel/ai sends the structured-output schema (`format`) on every agent step
// together with `tools`. Ollama constrains the whole reply to that JSON schema,
// so a model can never emit a tool call: the AI self-healer never fetched a
// page and returned guessed selectors (Chalkys was "healed" into recording
// pence as pounds that way). Upstream laravel/ai main still does this.
//
// With both present, the step is sent without `format`, so the model can call
// tools. When it stops calling tools, one more request with `format` turns its
// answer into the schema-shaped JSON laravel/ai parses. Requests without tools
// (plain structured extraction) and streaming requests pass through untouched.

use GuzzleHttp\Psr7\Utils;

return function (callable $handler): callable {
    return function ($request, array $options) use ($handler) {
        if (! str_ends_with($request->getUri()->getPath(), '/api/chat')) {
            return $handler($request, $options);
        }

        $body = json_decode((string) $request->getBody(), true);

        if (! is_array($body) || empty($body['tools']) || empty($body['format']) || ! empty($body['stream'])) {
            return $handler($request, $options);
        }

        $format = $body['format'];
        unset($body['format']);

        return $handler($request->withBody(Utils::streamFor(json_encode($body))), $options)
            ->then(function ($response) use ($handler, $request, $options, $body, $format) {
                if ($response->getStatusCode() !== 200) {
                    return $response;
                }

                $data = json_decode((string) $response->getBody(), true);
                $response->getBody()->rewind();

                if (! empty($data['message']['tool_calls'])) {
                    return $response;
                }

                $body['messages'][] = ['role' => 'assistant', 'content' => $data['message']['content'] ?? ''];
                $body['messages'][] = ['role' => 'user', 'content' => 'Now return the final extraction plan in the required JSON format. Use only selectors/regexes you validated with the test tools.'];
                $body['format'] = $format;

                return $handler($request->withBody(Utils::streamFor(json_encode($body))), $options);
            });
    };
};
