using System.Net;
using Markdig;

namespace RemoteNotifications.Surface.Services;

internal static class RemoteNotificationHtmlDocument
{
    private static readonly MarkdownPipeline Pipeline = new MarkdownPipelineBuilder()
        .UseAdvancedExtensions().DisableHtml().Build();

    internal static string Build(string label, string markdown, bool dark)
    {
        var body = Markdown.ToHtml(markdown, Pipeline);
        if (!string.IsNullOrWhiteSpace(label))
            body = $"<div class=\"label\">{WebUtility.HtmlEncode(label)}</div>{body}";
        return Template.Replace("__THEME__", dark ? "dark" : "light", StringComparison.Ordinal)
            .Replace("{{CONTENT}}", body, StringComparison.Ordinal);
    }

    private const string Template = """
        <!doctype html>
        <html data-theme="__THEME__">
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <style>
            html[data-theme="light"] { --bg: #FFFFFF; --fg: #1F2328; --muted: #656D76; --border: #D0D7DE; --code-bg: #F6F8FA; --link: #0969DA; --selection: rgba(9, 105, 218, 0.25); }
            html[data-theme="dark"] { --bg: #1E1E1E; --fg: #E6EDF3; --muted: #9198A1; --border: #3D444D; --code-bg: #2D333B; --link: #539BF5; --selection: rgba(83, 155, 245, 0.30); }
            html, body { background: var(--bg); color: var(--fg); }
            body { margin: 0; padding: 16px; font-family: "Segoe UI", "Helvetica Neue", Arial, sans-serif; font-size: 14px; line-height: 1.55; overflow-wrap: break-word; }
            .label { margin-bottom: 10px; font-size: 12px; font-weight: 600; letter-spacing: 0.06em; text-transform: uppercase; color: var(--muted); }
            h1, h2, h3, h4 { line-height: 1.3; margin: 1em 0 0.5em; }
            h1 { font-size: 20px; }
            h2 { font-size: 17px; }
            h3 { font-size: 15px; }
            p { margin: 0.5em 0; }
            ul, ol { margin: 0.5em 0; padding-left: 1.5em; }
            li { margin: 0.2em 0; }
            pre { margin: 0.5em 0; padding: 10px; overflow: auto; background: var(--code-bg); border: 1px solid var(--border); border-radius: 6px; font-size: 12.5px; }
            code { padding: 0.1em 0.35em; border-radius: 4px; background: var(--code-bg); font-family: "Cascadia Code", Consolas, monospace; font-size: 0.9em; }
            pre code { padding: 0; background: transparent; }
            blockquote { margin: 0.5em 0; padding-left: 1em; border-left: 3px solid var(--border); color: var(--muted); }
            table { width: 100%; margin: 0.5em 0; border-collapse: collapse; }
            th, td { padding: 6px 10px; border: 1px solid var(--border); text-align: left; }
            th { background: var(--code-bg); }
            a { color: var(--link); }
            hr { margin: 1em 0; border: none; border-top: 1px solid var(--border); }
            .task-list-item { list-style: none; }
            .task-list-item input { margin-right: 0.4em; }
            img { max-width: 100%; }
            ::selection { background: var(--selection); }
          </style>
        </head>
        <body>
        {{CONTENT}}
        <script>
          function post(message) {
            if (window.chrome && window.chrome.webview) {
              window.chrome.webview.postMessage(message);
              return;
            }
            if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.mptBridge) {
              window.webkit.messageHandlers.mptBridge.postMessage(message);
              return;
            }
            if (typeof window.invokeCSharpAction === "function") {
              window.invokeCSharpAction(message);
            }
          }
          function openClickedLink(event) {
            if (!event.isTrusted || (event.button !== 0 && event.button !== 1)) { return; }
            var anchor = event.target.closest && event.target.closest("a[href]");
            if (!anchor || anchor.getAttribute("href").startsWith("#")) { return; }
            var uri;
            try { uri = new URL(anchor.href, document.baseURI); } catch (_) { return; }
            if (uri.protocol !== "http:" && uri.protocol !== "https:") { return; }
            event.preventDefault();
            post("open-external:" + uri.href);
          }
          document.addEventListener("click", openClickedLink);
          document.addEventListener("auxclick", openClickedLink);
          function isEditable(node) {
            while (node) {
              if (node.isContentEditable) { return true; }
              var tag = node.tagName;
              if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") { return true; }
              node = node.parentElement;
            }
            return false;
          }
          document.addEventListener("keydown", function (event) {
            if (event.key === "Escape") {
              event.preventDefault();
              post("close");
              return;
            }
            if (isEditable(event.target)) { return; }
            if (event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) { return; }
            if (event.key === "ArrowLeft") {
              event.preventDefault();
              post("previous");
            } else if (event.key === "ArrowRight") {
              event.preventDefault();
              post("next");
            }
          });
        </script>
        </body>
        </html>
        """;
}
