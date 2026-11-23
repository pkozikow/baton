// Renders Markdown and Mermaid diagrams and loads pull request state. Pages stay readable without it.
(function () {
  'use strict';

  function markdownRenderer() {
    if (typeof window.markdownit !== 'function') {
      return null;
    }
    // Raw HTML stays disabled: artifacts, board posts and saved reviews are untrusted text.
    const md = window.markdownit({ html: false, linkify: false, typographer: false });
    // Only relative references and http(s)/mailto links survive; markdown-it's default also admits data: images.
    md.validateLink = function (url) {
      const value = url.trim();
      return !/^[a-z][a-z0-9+.-]*:/i.test(value) || /^(https?|mailto):/i.test(value);
    };
    // An inline style attribute would violate the page's CSP, so table alignment becomes a class.
    for (const rule of ['th_open', 'td_open']) {
      md.renderer.rules[rule] = function (tokens, index, options, env, self) {
        const token = tokens[index];
        const style = token.attrGet('style');
        if (style !== null) {
          token.attrs = token.attrs.filter(function (attr) { return attr[0] !== 'style'; });
          const align = /text-align:\s*(left|right|center)/.exec(style);
          if (align) {
            token.attrJoin('class', 'align-' + align[1]);
          }
        }
        return self.renderToken(tokens, index, options);
      };
    }
    // Artifact files are never served raw and remote images are blocked, so images become links.
    md.renderer.rules.image = function (tokens, index, options, env, self) {
      const token = tokens[index];
      const source = token.attrGet('src') || '';
      const text = self.renderInlineAsText(token.children || [], options, env) || source;
      return '<a href="' + md.utils.escapeHtml(source) + '">[image: ' + md.utils.escapeHtml(text) + ']</a>';
    };
    return md;
  }

  function renderMarkdown() {
    const md = markdownRenderer();
    if (md === null) {
      return;
    }
    for (const source of document.querySelectorAll('pre[data-markdown]')) {
      const rendered = document.createElement('div');
      rendered.className = 'markdown';
      rendered.innerHTML = md.render(source.textContent);
      source.replaceWith(rendered);
    }
  }

  function loadScript(url) {
    return new Promise(function (resolve, reject) {
      const script = document.createElement('script');
      script.src = url;
      script.onload = resolve;
      script.onerror = function () { reject(new Error('Cannot load ' + url)); };
      document.head.appendChild(script);
    });
  }

  async function renderDiagrams() {
    const url = document.body.dataset.mermaid;
    const blocks = Array.from(document.querySelectorAll('.markdown pre > code.language-mermaid'));
    if (blocks.length === 0) {
      return;
    }
    if (!url) {
      // The server marks pages with diagrams; a block it missed must not look like plain code.
      for (const code of blocks) {
        showDiagramError(code.parentElement, new Error('this page was not prepared for diagrams'));
      }
      return;
    }
    try {
      await loadScript(url);
    } catch (error) {
      for (const code of blocks) {
        showDiagramError(code.parentElement, error);
      }
      return;
    }
    const mermaid = window.mermaid;
    // strict sanitizes labels and drops click handlers. SVG text labels keep label HTML out of the DOM
    // entirely; diagrams may not change these settings.
    mermaid.initialize({
      startOnLoad: false,
      securityLevel: 'strict',
      suppressErrorRendering: true,
      htmlLabels: false,
      flowchart: { htmlLabels: false },
      secure: ['secure', 'securityLevel', 'startOnLoad', 'maxTextSize', 'suppressErrorRendering', 'maxEdges',
        'dompurifyConfig', 'htmlLabels', 'flowchart'],
    });
    let index = 0;
    for (const code of blocks) {
      const id = 'baton-mermaid-' + index++;
      const pre = code.parentElement;
      try {
        const result = await mermaid.render(id, code.textContent);
        const figure = document.createElement('div');
        figure.className = 'mermaid-diagram';
        figure.innerHTML = result.svg;
        pre.replaceWith(figure);
      } catch (error) {
        const leftover = document.getElementById('d' + id);
        if (leftover) {
          leftover.remove();
        }
        showDiagramError(pre, error);
      }
    }
  }

  function showDiagramError(pre, error) {
    const note = document.createElement('p');
    note.className = 'error diagram-error';
    note.textContent = 'Diagram not rendered: ' + (error && error.message ? error.message : String(error));
    pre.after(note);
  }

  function loadPullRequests() {
    for (const holder of document.querySelectorAll('[data-pull-requests]')) {
      holder.textContent = 'Looking up pull requests…';
      fetch(holder.dataset.pullRequests)
        .then(function (response) {
          if (!response.ok) {
            throw new Error('HTTP ' + response.status);
          }
          return response.text();
        })
        .then(function (fragment) { holder.innerHTML = fragment; })
        .catch(function () { holder.textContent = 'Pull requests unavailable.'; });
    }
  }

  renderMarkdown();
  loadPullRequests();
  renderDiagrams();
})();
