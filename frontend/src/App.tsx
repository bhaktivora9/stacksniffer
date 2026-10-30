import type { ReactNode } from "react";
import { BrowserRouter, Routes, Route, useLocation } from "react-router-dom";
import ErrorBoundary from "./components/ErrorBoundary";
import HomePage from "./pages/HomePage";
import AnalysisPage from "./pages/AnalysisPage";

// Each page renders inside its own boundary, cleared on navigation, so one page's render error
// cannot blank the app.
function Guarded({ children }: { children: ReactNode }) {
  const location = useLocation();
  return <ErrorBoundary resetKey={location.key}>{children}</ErrorBoundary>;
}

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Guarded><HomePage /></Guarded>} />
        <Route path="/analyses/:analysisId" element={<Guarded><AnalysisPage /></Guarded>} />
      </Routes>
    </BrowserRouter>
  );
}
