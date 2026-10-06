// API client built from the service schema; regenerate it instead of editing by hand.
export const API_BASE_URL = "/v1";

export const fetchUser = (id: number): Promise<Response> => fetch(`${API_BASE_URL}/users/${id}`);
