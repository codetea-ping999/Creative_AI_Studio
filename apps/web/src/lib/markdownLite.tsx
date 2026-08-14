import { Fragment, type ReactNode } from "react";

/**
 * A small, dependency-free renderer for the markdown this studio's own text
 * generators emit (see generators/text/tasks.py): headers, ordered/unordered
 * lists with one level of indentation, bold/italic spans, and paragraphs.
 * Not a CommonMark implementation — just enough to make generated loglines,
 * beat sheets, and scene lists readable instead of a raw code block.
 */
export function renderMarkdownLite(source: string): ReactNode[] {
  const lines = source.replace(/\r\n/g, "\n").split("\n");
  const blocks: ReactNode[] = [];
  let listItems: { text: string; indented: boolean }[] = [];
  let paragraphLines: string[] = [];
  let blockKey = 0;

  function flushList(): void {
    if (listItems.length === 0) {
      return;
    }
    blockKey += 1;
    blocks.push(
      <ul className="markdown-lite__list" key={`list-${blockKey}`}>
        {listItems.map((item, index) => (
          <li
            key={index}
            className={item.indented ? "markdown-lite__list-item is-nested" : "markdown-lite__list-item"}
          >
            {renderInline(item.text)}
          </li>
        ))}
      </ul>,
    );
    listItems = [];
  }

  function flushParagraph(): void {
    if (paragraphLines.length === 0) {
      return;
    }
    blockKey += 1;
    blocks.push(
      <p className="markdown-lite__paragraph" key={`p-${blockKey}`}>
        {renderInline(paragraphLines.join(" "))}
      </p>,
    );
    paragraphLines = [];
  }

  for (const rawLine of lines) {
    const line = rawLine.trimEnd();
    const headerMatch = /^(#{1,6})\s+(.*)$/.exec(line);
    const listMatch = /^(\s*)[-*]\s+(.*)$/.exec(line) ?? /^(\s*)\d+\.\s+(.*)$/.exec(line);

    if (line.trim() === "") {
      flushList();
      flushParagraph();
      continue;
    }
    if (headerMatch) {
      flushList();
      flushParagraph();
      blockKey += 1;
      blocks.push(
        renderHeading(Math.min(headerMatch[1].length + 2, 6), headerMatch[2], `h-${blockKey}`),
      );
      continue;
    }
    if (listMatch) {
      flushParagraph();
      listItems.push({ text: listMatch[2], indented: listMatch[1].length > 0 });
      continue;
    }
    flushList();
    paragraphLines.push(line.trim());
  }
  flushList();
  flushParagraph();
  return blocks;
}

function renderHeading(level: number, text: string, key: string): ReactNode {
  const content = renderInline(text);
  switch (level) {
    case 3:
      return (
        <h3 className="markdown-lite__heading" key={key}>
          {content}
        </h3>
      );
    case 4:
      return (
        <h4 className="markdown-lite__heading" key={key}>
          {content}
        </h4>
      );
    case 5:
      return (
        <h5 className="markdown-lite__heading" key={key}>
          {content}
        </h5>
      );
    case 6:
      return (
        <h6 className="markdown-lite__heading" key={key}>
          {content}
        </h6>
      );
    default:
      return (
        <h2 className="markdown-lite__heading" key={key}>
          {content}
        </h2>
      );
  }
}

function renderInline(text: string): ReactNode {
  const parts = text.split(/(\*\*[^*]+\*\*|\*[^*]+\*)/g).filter((part) => part !== "");
  return (
    <Fragment>
      {parts.map((part, index) => {
        if (part.startsWith("**") && part.endsWith("**")) {
          return <strong key={index}>{part.slice(2, -2)}</strong>;
        }
        if (part.startsWith("*") && part.endsWith("*")) {
          return <em key={index}>{part.slice(1, -1)}</em>;
        }
        return <Fragment key={index}>{part}</Fragment>;
      })}
    </Fragment>
  );
}
