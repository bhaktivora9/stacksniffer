import { Component } from "react";

/**
 * Catches a render error below it and shows `fallback({ error, reset })` instead of a blank page.
 * Changing `resetKey` (the route, for example) clears the error.
 */
export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null, resetKey: props.resetKey };
    this.reset = () => this.setState({ error: null });
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  static getDerivedStateFromProps(props, state) {
    return props.resetKey === state.resetKey ? null : { error: null, resetKey: props.resetKey };
  }

  componentDidCatch(error, info) {
    console.error("Unhandled render error", error, info.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    if (this.props.fallback) return this.props.fallback({ error, reset: this.reset });
    return <div className="app-crash" role="alert">
      <h1>Something went wrong</h1>
      <pre>{String(error?.message || error)}</pre>
      <div>
        <button type="button" onClick={this.reset}>try again</button>
        <a href="/">back to home</a>
      </div>
    </div>;
  }
}
