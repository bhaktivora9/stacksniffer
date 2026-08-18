import { BrowserRouter, Routes, Route } from "react-router-dom";
import HomePage from "./pages/HomePage";
import ResultsPage from "./pages/ResultsPage";
import ReviewQueuePage from "./pages/ReviewQueuePage";
import RepositoryHistoryPage from "./pages/RepositoryHistoryPage";
import AnalyticsPage from "./pages/AnalyticsPage";
import SearchPage from "./pages/SearchPage";

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<HomePage />} />
        <Route path="/results/:analysisId" element={<ResultsPage />} />
        <Route path="/review" element={<ReviewQueuePage />} />
        <Route path="/repositories" element={<RepositoryHistoryPage />} />
        <Route path="/analytics" element={<AnalyticsPage />} />
        <Route path="/search" element={<SearchPage />} />
      </Routes>
    </BrowserRouter>
  );
}
