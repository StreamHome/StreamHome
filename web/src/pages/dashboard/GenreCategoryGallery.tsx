import React, { useId } from "react";
import { motion } from "framer-motion";
import { MediaArtwork } from "../../components/media/MediaArtwork";
import { CONTENT_REVEAL, CONTENT_STAGGER } from "../../motion/motionSystem";
import type { GenreCategoryCard } from "./catalogPresentation";

export function GenreCategoryGallery({
  cards,
  active,
  variant,
  onSelect,
}: {
  cards: GenreCategoryCard[];
  active: string;
  variant: "shared" | "ember";
  onSelect: (category: string) => void;
}) {
  const headingId = useId();
  if (!cards.length) return null;

  return <motion.section variants={CONTENT_REVEAL} className={`genre-category-gallery genre-category-gallery--${variant}`} aria-labelledby={headingId}>
    <header>
      <div><p>EXPLORE THE LIBRARY</p><h2 id={headingId}>Browse Categories</h2></div>
      <span>{cards.length} genre{cards.length === 1 ? "" : "s"}</span>
    </header>
    <motion.div variants={CONTENT_STAGGER} initial="hidden" animate="shown" className="genre-category-gallery__grid">
      {cards.map((card, index) => {
        const selected = card.value.toLocaleLowerCase() === active.toLocaleLowerCase();
        return <motion.button
          variants={CONTENT_REVEAL}
          key={card.value}
          type="button"
          data-active={selected}
          data-category-index={String(index + 1).padStart(2, "0")}
          aria-pressed={selected}
          aria-label={`${card.label}, ${card.count} title${card.count === 1 ? "" : "s"}`}
          onClick={() => onSelect(card.value)}
        >
          <span className="genre-category-gallery__art" aria-hidden="true">
            <MediaArtwork src={card.representative.bannerUrl || card.representative.thumbnailUrl} alt="" media={card.representative} className="genre-category-gallery__image h-full w-full object-cover" />
          </span>
          <span className="genre-category-gallery__shade" aria-hidden="true" />
          <span className="genre-category-gallery__copy"><strong>{card.label}</strong><small>{card.count} title{card.count === 1 ? "" : "s"}</small></span>
        </motion.button>;
      })}
    </motion.div>
  </motion.section>;
}
