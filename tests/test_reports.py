"""Report readability and the boundary between model text and executable HTML."""
from html.parser import HTMLParser
import unittest

from research_agent.reports import render_report


class Document(HTMLParser):
    def __init__(self, content):
        super().__init__(); self.elements = []; self.feed(content)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


class ReportTests(unittest.TestCase):
    def test_report_renders_sections_tables_nested_lists_code_and_citations(self):
        content = '''# 研究报告

Memory research

## 结果

**跨任务经验**与 *反馈* [S7][E4]。

| 方法 | 机制 |
| --- | --- |
| ExpeL | 保存 a\\|b |

3. 第一项
   - 子项 `a < b`
4. 第二项

> 阅读摘要，核对方法。

```python
print("<script>literal</script>")
```

## 来源

- [S7] Paper — https://arxiv.org/abs/2308.10144
'''
        page = render_report(content, 'Memory research')
        self.assertIn('<strong>跨任务经验</strong>', page)
        self.assertIn('<em>反馈</em>', page)
        self.assertIn('<table>', page)
        self.assertIn('<td>保存 a|b</td>', page)
        self.assertIn('<ol start="3">', page)
        self.assertIn('<ul><li>子项 <code>a &lt; b</code></li></ul>', page)
        self.assertIn('<blockquote>', page)
        self.assertIn('href="#source-S7"', page)
        self.assertIn('id="source-S7"', page)
        self.assertNotIn('href="#source-E4"', page)
        self.assertIn('href="https://arxiv.org/abs/2308.10144"', page)
        self.assertIn('href="#section-1"', page)
        self.assertEqual(page.count('<h1>'), 1)
        self.assertNotIn('<pre># 研究报告', page)

    def test_untrusted_markup_and_unsafe_links_are_not_executable(self):
        page = render_report('''## <img src=x onerror=alert(1)>

<script>alert(1)</script>

[bad](javascript:alert(1)) [data](data:text/html,bad) [local](file:///C:/secret)
[credentials](https://user:pass@example.org/) [ok](https://example.org/?q="quoted")
`**literal** <img src=x>`
''', '<svg onload=alert(1)>')
        self.assertIn('&lt;script&gt;', page)
        self.assertIn('<code>**literal** &lt;img src=x&gt;</code>', page)
        for tag, attrs in Document(page).elements:
            self.assertNotIn(tag, {'script', 'img', 'iframe', 'svg', 'object'})
            for key, value in attrs.items():
                if key.startswith('on'):
                    self.assertEqual((tag, key, value), ('button', 'onclick', 'window.print()'))
            if tag == 'a':
                self.assertTrue(attrs['href'].startswith(('#', 'http://', 'https://')))
                self.assertNotIn('user:pass', attrs['href'])

    def test_scoped_child_report_links_render_without_allowing_arbitrary_paths(self):
        url = '/api/spaces/' + 'a' * 32 + '/jobs/' + 'b' * 32 + '/report.html'
        bad = [url + '?path=.env', url.replace('/report.html', '/../.env'), '/api/spaces/a/jobs/b/report.html']
        page = render_report('[child](' + url + ') ' + ' '.join('[bad](' + value + ')' for value in bad))
        self.assertIn('href="' + url + '"', page)
        for value in bad: self.assertNotIn('href="' + value + '"', page)

    def test_repeated_source_labels_do_not_create_duplicate_targets(self):
        page = render_report('## 来源\n\n- [S2] first https://example.org/a\n- [S2] repeated\n\n[missing](https://example.org/b).')
        ids = [attrs['id'] for _, attrs in Document(page).elements if 'id' in attrs]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn('rel="noopener noreferrer"', page)


if __name__ == '__main__':
    unittest.main()
