import { apiGet } from "./client";
import type { Movie, DiscoverMovie, Episode } from "../types/api";

export const getMovies = () => apiGet<Movie[]>("/api/movies");
export const getMovie = (id: string) => apiGet<Movie>(`/api/movies/${id}`);
export const getFeatured = () => apiGet<Movie | null>("/api/movies/featured");
export const search = (query: string) => apiGet<DiscoverMovie[]>(`/api/search?query=${encodeURIComponent(query)}`);
export const discover = (category: string, type: string) => apiGet<DiscoverMovie[]>(`/api/discover?category=${encodeURIComponent(category)}&type=${encodeURIComponent(type)}`);
export const getEpisodes = (tmdbId: number | string) => apiGet<Episode[]>(`/api/series/${tmdbId}/episodes`);
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export const getTmdbDetails = (type: string, id: number | string) => apiGet<any>(`/api/tmdb/${type}/${id}`);
