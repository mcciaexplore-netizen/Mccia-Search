"use client";

import { useCallback, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import { stripCitations } from "@/lib/citations";
import { PHOTOS_FOUND_TEXT, photosAreTheAnswer } from "@/lib/photos";
import type { Message } from "@/lib/types";
import PhotoResults from "./PhotoResults";
import Sources from "./Sources";

function formatTime(date: Date): string {
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export default function MessageBubble({ message }: { message: Message }) {
  // If none of the photographs can be loaded they are no answer after all: the usual "not found" shows instead.
  const [photosFailed, setPhotosFailed] = useState(false);
  const onPhotosFailed = useCallback(() => setPhotosFailed(true), []);
  // The answer is drawn without its bracketed citations; the message itself is kept as the server wrote it.
  const text = useMemo(() => stripCitations(message.content), [message.content]);
  const time = <span className="mt-1.5 px-1 text-xs text-muted">{formatTime(message.timestamp)}</span>;

  if (message.role === "user") {
    return (
      <div className="flex animate-message-in flex-col items-end">
        <div className="max-w-[88%] whitespace-pre-wrap break-words rounded-2xl rounded-br-md bg-accent px-4 py-3 text-base leading-relaxed text-white md:max-w-[80%]">
          {message.content}
        </div>
        {time}
      </div>
    );
  }

  // The text search found nothing but photographs were found: they are the answer, so say that, not "not found".
  const photosAnswer = photosAreTheAnswer(message) && !photosFailed;
  const photos = message.photos && message.photos.length > 0 ? message.photos : null;

  return (
    <div className="flex animate-message-in flex-col items-start">
      <div className="w-full rounded-2xl rounded-bl-md border border-line bg-white px-5 py-4 text-ink">
        {photosAnswer ? (
          <p className="text-base font-semibold leading-relaxed text-ink">{PHOTOS_FOUND_TEXT}</p>
        ) : (
          <div className="prose max-w-none break-words">
            {/* Images are not rendered: a remote image URL in a model answer could leak data. */}
            <ReactMarkdown disallowedElements={["img"]}>{text}</ReactMarkdown>
          </div>
        )}
        {message.found === false && !message.clarify && !photosAnswer && (
          <p className="mt-3 flex items-center gap-1.5 text-sm text-muted">
            <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-muted/60" />
            No exact match found
          </p>
        )}
        {photos && <PhotoResults photos={photos} showHeading={!photosAnswer} onAllBroken={onPhotosFailed} />}
        {message.found === true && <Sources sources={message.sources} />}
      </div>
      {time}
    </div>
  );
}
